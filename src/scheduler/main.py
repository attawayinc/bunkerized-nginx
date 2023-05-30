#!/usr/bin/python3

from argparse import ArgumentParser
from glob import glob
from hashlib import sha256
from io import BytesIO
from json import load as json_load
from os import (
    _exit,
    chmod,
    environ,
    getenv,
    getpid,
    listdir,
    sep,
    walk,
)
from os.path import basename, dirname, join, normpath
from pathlib import Path
from shutil import copy, rmtree
from signal import SIGINT, SIGTERM, signal, SIGHUP
from stat import S_IEXEC
from subprocess import run as subprocess_run, DEVNULL, STDOUT
from sys import path as sys_path
from tarfile import open as tar_open
from time import sleep
from traceback import format_exc
from typing import Any, Dict, List, Optional, Union

for deps_path in [
    join(sep, "usr", "share", "bunkerweb", *paths)
    for paths in (("deps", "python"), ("utils",), ("api",), ("db",))
]:
    if deps_path not in sys_path:
        sys_path.append(deps_path)

from dotenv import dotenv_values

from logger import setup_logger  # type: ignore
from Database import Database  # type: ignore
from JobScheduler import JobScheduler
from ApiCaller import ApiCaller  # type: ignore

RUN = True
SCHEDULER: Optional[JobScheduler] = None
GENERATE = False
INTEGRATION = "Linux"
CACHE_PATH = join(sep, "var", "cache", "bunkerweb")
logger = setup_logger("Scheduler", getenv("LOG_LEVEL", "INFO"))


def handle_stop(signum, frame):
    if SCHEDULER is not None:
        SCHEDULER.clear()
    stop(0)


signal(SIGINT, handle_stop)
signal(SIGTERM, handle_stop)


# Function to catch SIGHUP and reload the scheduler
def handle_reload(signum, frame):
    try:
        if SCHEDULER is not None and RUN:
            # Get the env by reading the .env file
            tmp_env = dotenv_values(join(sep, "etc", "bunkerweb", "variables.env"))
            if SCHEDULER.reload(tmp_env):
                logger.info("Reload successful")
            else:
                logger.error("Reload failed")
        else:
            logger.warning(
                "Ignored reload operation because scheduler is not running ...",
            )
    except:
        logger.error(
            f"Exception while reloading scheduler : {format_exc()}",
        )


signal(SIGHUP, handle_reload)


def stop(status):
    Path(sep, "var", "tmp", "bunkerweb", "scheduler.pid").unlink(missing_ok=True)
    Path(sep, "var", "tmp", "bunkerweb", "scheduler.healthy").unlink(missing_ok=True)
    _exit(status)


def generate_custom_configs(
    configs: List[Dict[str, Any]],
    *,
    original_path: Union[Path, str] = join(sep, "etc", "bunkerweb", "configs"),
):
    if not isinstance(original_path, Path):
        original_path = Path(original_path)

    # Remove old custom configs files
    logger.info("Removing old custom configs files ...")
    for file in glob(str(original_path.joinpath("*", "*"))):
        file = Path(file)
        if file.is_symlink() or file.is_file():
            file.unlink()
        elif file.is_dir():
            rmtree(str(file), ignore_errors=True)

    if configs:
        logger.info("Generating new custom configs ...")
        original_path.mkdir(parents=True, exist_ok=True)
        for custom_config in configs:
            tmp_path = original_path.joinpath(
                custom_config["type"].replace("_", "-"),
                custom_config["service_id"] or "",
                f"{custom_config['name']}.conf",
            )
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(custom_config["data"])

        if SCHEDULER.apis:
            logger.info("Sending custom configs to BunkerWeb")
            ret = SCHEDULER.send_files(original_path, "/custom_configs")

            if not ret:
                logger.error(
                    "Sending custom configs failed, configuration will not work as expected...",
                )


def generate_external_plugins(
    plugins: List[Dict[str, Any]],
    *,
    original_path: Union[Path, str] = join(sep, "etc", "bunkerweb", "plugins"),
):
    if not isinstance(original_path, Path):
        original_path = Path(original_path)

    # Remove old external plugins files
    logger.info("Removing old external plugins files ...")
    for file in glob(str(original_path.joinpath("*"))):
        file = Path(file)
        if file.is_symlink() or file.is_file():
            file.unlink()
        elif file.is_dir():
            rmtree(str(file), ignore_errors=True)

    if plugins:
        logger.info("Generating new external plugins ...")
        original_path.mkdir(parents=True, exist_ok=True)
        for plugin in plugins:
            tmp_path = original_path.joinpath(plugin["id"], f"{plugin['name']}.tar.gz")
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(plugin["data"])
            with tar_open(str(tmp_path), "r:gz") as tar:
                tar.extractall(original_path)
            tmp_path.unlink()

            for job_file in glob(join(str(tmp_path.parent), "jobs", "*")):
                st = Path(job_file).stat()
                chmod(job_file, st.st_mode | S_IEXEC)

        if SCHEDULER.apis:
            logger.info("Sending plugins to BunkerWeb")
            ret = SCHEDULER.send_files(original_path, "/plugins")

            if not ret:
                logger.error(
                    "Sending plugins failed, configuration will not work as expected...",
                )


if __name__ == "__main__":
    try:
        # Don't execute if pid file exists
        pid_path = Path(sep, "var", "tmp", "bunkerweb", "scheduler.pid")
        if pid_path.is_file():
            logger.error(
                "Scheduler is already running, skipping execution ...",
            )
            _exit(1)

        # Write pid to file
        pid_path.write_text(str(getpid()), encoding="utf-8")

        del pid_path

        # Parse arguments
        parser = ArgumentParser(description="Job scheduler for BunkerWeb")
        parser.add_argument(
            "--variables",
            type=str,
            help="path to the file containing environment variables",
        )
        args = parser.parse_args()
        tmp_variables_path = Path(
            normpath(args.variables) if args.variables else sep,
            "var",
            "tmp",
            "bunkerweb",
            "variables.env",
        )
        dotenv_env = dotenv_values(str(tmp_variables_path))

        logger.info("Scheduler started ...")

        # Checking if the argument variables is true.
        if args.variables:
            logger.info(f"Variables : {tmp_variables_path}")

            # Read env file
            env = dotenv_env.copy()

            db = Database(
                logger,
                sqlalchemy_string=env.get("DATABASE_URI", getenv("DATABASE_URI", None)),
            )

            while not db.is_initialized():
                logger.warning(
                    "Database is not initialized, retrying in 5s ...",
                )
                sleep(5)

            db_configs = db.get_custom_configs()
        else:
            # Read from database
            INTEGRATION = "Docker"
            integration_path = Path(sep, "usr", "share", "bunkerweb", "INTEGRATION")
            if integration_path.is_file():
                INTEGRATION = integration_path.read_text(encoding="utf-8").strip()

            del integration_path

            db = Database(
                logger,
                sqlalchemy_string=getenv("DATABASE_URI", None),
            )

            if INTEGRATION in (
                "Swarm",
                "Kubernetes",
                "Autoconf",
            ):
                while not db.is_autoconf_loaded():
                    logger.warning(
                        "Autoconf is not loaded yet in the database, retrying in 5s ...",
                    )
                    sleep(5)
            elif not tmp_variables_path.is_file() or db.get_config() != dotenv_env:
                # run the config saver
                proc = subprocess_run(
                    [
                        "python",
                        join(sep, "usr", "share", "bunkerweb", "gen", "save_config.py"),
                        "--settings",
                        join(sep, "usr", "share", "bunkerweb", "settings.json"),
                    ],
                    stdin=DEVNULL,
                    stderr=STDOUT,
                    check=False,
                )
                if proc.returncode != 0:
                    logger.error(
                        "Config saver failed, configuration will not work as expected...",
                    )

            while not db.is_initialized():
                logger.warning(
                    "Database is not initialized, retrying in 5s ...",
                )
                sleep(5)

            env = db.get_config()
            while not db.is_first_config_saved() or not env:
                logger.warning(
                    "Database doesn't have any config saved yet, retrying in 5s ...",
                )
                sleep(5)
                env = db.get_config()

            env["DATABASE_URI"] = db.get_database_uri()

        # Checking if any custom config has been created by the user
        custom_configs = []
        db_configs = db.get_custom_configs()
        configs_path = Path(sep, "etc", "bunkerweb", "configs")
        root_dirs = listdir(str(configs_path))
        for root, dirs, files in walk(str(configs_path)):
            if files or (dirs and basename(root) not in root_dirs):
                path_exploded = root.split("/")
                for file in files:
                    with open(join(root, file), "r", encoding="utf-8") as f:
                        custom_conf = {
                            "value": f.read(),
                            "exploded": (
                                f"{path_exploded.pop()}"
                                if path_exploded[-1] not in root_dirs
                                else None,
                                path_exploded[-1],
                                file.replace(".conf", ""),
                            ),
                        }

                    saving = True
                    for db_conf in db_configs:
                        if (
                            db_conf["method"] != "manual"
                            and db_conf["service_id"] == custom_conf["exploded"][0]
                            and db_conf["name"] == custom_conf["exploded"][2]
                        ):
                            saving = False
                            break

                    if saving:
                        custom_configs.append(custom_conf)

        err = db.save_custom_configs(custom_configs, "manual")
        if err:
            logger.error(
                f"Couldn't save some manually created custom configs to database: {err}",
            )

        generate_custom_configs(db.get_custom_configs(), original_path=configs_path)

        # Check if any external plugin has been added by the user
        external_plugins = []
        plugins_dir = Path(sep, "etc", "bunkerweb", "plugins")
        for filename in glob(str(plugins_dir.joinpath("*", "plugin.json"))):
            with open(filename, "r", encoding="utf-8") as f:
                _dir = dirname(filename)
                plugin_content = BytesIO()
                with tar_open(
                    fileobj=plugin_content, mode="w:gz", compresslevel=9
                ) as tar:
                    tar.add(_dir, arcname=basename(_dir), recursive=True)
                plugin_content.seek(0)
                value = plugin_content.getvalue()

                external_plugins.append(
                    json_load(f)
                    | {
                        "external": True,
                        "page": Path(_dir, "ui").exists(),
                        "method": "manual",
                        "data": value,
                        "checksum": sha256(value).hexdigest(),
                    }
                )

        if external_plugins:
            err = db.update_external_plugins(external_plugins, delete_missing=False)
            if err:
                logger.error(
                    f"Couldn't save some manually added plugins to database: {err}",
                )

        generate_external_plugins(
            db.get_plugins(external=True, with_data=True),
            original_path=plugins_dir,
        )

        logger.info("Executing scheduler ...")

        GENERATE = not tmp_variables_path.exists() or env != dotenv_env

        del dotenv_env

        if not GENERATE:
            logger.warning(
                "Looks like BunkerWeb configuration is already generated, will not GENERATE it again ..."
            )

        # Instantiate scheduler
        SCHEDULER = JobScheduler(env.copy() | environ.copy(), logger, INTEGRATION)
        # Automatically setup the scheduler apis
        SCHEDULER.auto_setup(bw_integration=INTEGRATION)

        FIRST_RUN = True
        while True:
            ret = db.checked_changes()

            if ret:
                logger.error(
                    f"An error occurred when setting the changes to checked in the database : {ret}"
                )
                stop(1)

            # Update the environment variables of the scheduler
            SCHEDULER.env = env.copy() | environ.copy()

            # Only run jobs once
            if not SCHEDULER.run_once():
                logger.error("At least one job in run_once() failed")
            else:
                logger.info("All jobs in run_once() were successful")

            changes = db.check_changes()

            if isinstance(changes, str):
                logger.error(
                    f"An error occurred when checking for changes in the database : {changes}"
                )
                stop(1)

            # check if the plugins have changed since last time
            if changes["external_plugins_changed"]:
                logger.info("External plugins changed, generating ...")
                generate_external_plugins(
                    db.get_plugins(external=True, with_data=True),
                    original_path=plugins_dir,
                )

                # run the config saver to save potential plugins settings
                proc = subprocess_run(
                    [
                        "python",
                        join(sep, "usr", "share", "bunkerweb", "gen", "save_config.py"),
                        "--settings",
                        join(sep, "usr", "share", "bunkerweb", "settings.json"),
                    ],
                    stdin=DEVNULL,
                    stderr=STDOUT,
                    check=False,
                )
                if proc.returncode != 0:
                    logger.error(
                        "Config saver failed, configuration will not work as expected...",
                    )

                ret = db.checked_changes()

                if ret:
                    logger.error(
                        f"An error occurred when setting the changes to checked in the database : {ret}"
                    )
                    stop(1)

            if GENERATE:
                # run the generator
                proc = subprocess_run(
                    [
                        "python3",
                        join(sep, "usr", "share", "bunkerweb", "gen", "main.py"),
                        "--settings",
                        join(sep, "usr", "share", "bunkerweb", "settings.json"),
                        "--templates",
                        join(sep, "usr", "share", "bunkerweb", "confs"),
                        "--output",
                        join(sep, "etc", "nginx"),
                    ]
                    + (
                        ["--variables", str(tmp_variables_path)]
                        if args.variables and FIRST_RUN
                        else []
                    ),
                    stdin=DEVNULL,
                    stderr=STDOUT,
                    check=False,
                )

                if proc.returncode != 0:
                    logger.error(
                        "Config generator failed, configuration will not work as expected...",
                    )
                else:
                    copy(
                        join(sep, "etc", "nginx", "variables.env"),
                        str(tmp_variables_path),
                    )

                    if SCHEDULER.apis:
                        # send nginx configs
                        logger.info(f"Sending {join(sep, 'etc', 'nginx')} folder ...")
                        ret = SCHEDULER.send_files(join(sep, "etc", "nginx"), "/confs")
                        if not ret:
                            logger.error(
                                "Sending nginx configs failed, configuration will not work as expected...",
                            )

            try:
                if SCHEDULER.apis:
                    # send cache
                    logger.info(f"Sending {CACHE_PATH} folder ...")
                    if not SCHEDULER.send_files(CACHE_PATH, "/cache"):
                        logger.error(f"Error while sending {CACHE_PATH} folder")
                    else:
                        logger.info(f"Successfully sent {CACHE_PATH} folder")

                    if SCHEDULER.send_to_apis("POST", "/reload"):
                        logger.info("Successfully reloaded nginx")
                    else:
                        logger.error("Error while reloading nginx")
                else:
                    # Stop temp nginx
                    logger.info("Stopping temp nginx ...")
                    proc = subprocess_run(
                        ["sudo", join(sep, "usr", "sbin", "nginx"), "-s", "stop"],
                        stdin=DEVNULL,
                        stderr=STDOUT,
                        env=env.copy(),
                        check=False,
                    )
                    if proc.returncode == 0:
                        logger.info("Successfully sent stop signal to temp nginx")
                        i = 0
                        while i < 20:
                            if not Path(
                                sep, "var", "tmp", "bunkerweb", "nginx.pid"
                            ).is_file():
                                break
                            logger.warning("Waiting for temp nginx to stop ...")
                            sleep(1)
                            i += 1
                        if i >= 20:
                            logger.error(
                                "Timeout error while waiting for temp nginx to stop"
                            )
                        else:
                            # Start nginx
                            logger.info("Starting nginx ...")
                            proc = subprocess_run(
                                ["sudo", join(sep, "usr", "sbin", "nginx")],
                                stdin=DEVNULL,
                                stderr=STDOUT,
                                env=env.copy(),
                                check=False,
                            )
                            if proc.returncode == 0:
                                logger.info("Successfully started nginx")
                            else:
                                logger.error(
                                    f"Error while starting nginx - returncode: {proc.returncode} - error: {proc.stderr.decode('utf-8') if proc.stderr else 'Missing stderr'}",
                                )
                    else:
                        logger.error(
                            f"Error while sending stop signal to temp nginx - returncode: {proc.returncode} - error: {proc.stderr.decode('utf-8') if proc.stderr else 'Missing stderr'}",
                        )
            except:
                logger.error(
                    f"Exception while reloading after running jobs once scheduling : {format_exc()}",
                )

            GENERATE = True
            SCHEDULER.setup()
            NEED_RELOAD = False
            CONFIGS_NEED_GENERATION = False
            PLUGINS_NEED_GENERATION = False
            FIRST_RUN = False

            # infinite schedule for the jobs
            logger.info("Executing job scheduler ...")
            Path(sep, "var", "tmp", "bunkerweb", "scheduler.healthy").write_text(
                "ok", encoding="utf-8"
            )
            while RUN and not NEED_RELOAD:
                SCHEDULER.run_pending()
                sleep(1)

                changes = db.check_changes()

                if isinstance(changes, str):
                    logger.error(
                        f"An error occurred when checking for changes in the database : {changes}"
                    )
                    stop(1)

                # check if the custom configs have changed since last time
                if changes["custom_configs_changed"]:
                    logger.info("Custom configs changed, generating ...")
                    CONFIGS_NEED_GENERATION = True
                    NEED_RELOAD = True

                # check if the plugins have changed since last time
                if changes["external_plugins_changed"]:
                    logger.info("External plugins changed, generating ...")
                    PLUGINS_NEED_GENERATION = True
                    NEED_RELOAD = True

                # check if the config have changed since last time
                if changes["config_changed"]:
                    logger.info("Config changed, generating ...")
                    NEED_RELOAD = True

            if NEED_RELOAD:
                if CONFIGS_NEED_GENERATION:
                    generate_custom_configs(
                        db.get_custom_configs(), original_path=configs_path
                    )

                if PLUGINS_NEED_GENERATION:
                    generate_external_plugins(
                        db.get_plugins(external=True, with_data=True),
                        original_path=plugins_dir,
                    )

                env = db.get_config()
    except:
        logger.error(
            f"Exception while executing scheduler : {format_exc()}",
        )
        stop(1)
