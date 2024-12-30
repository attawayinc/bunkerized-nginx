#!/usr/bin/env python3

from os import environ, getenv, sep
from os.path import join
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen
from sys import exit as sys_exit, path as sys_path

for deps_path in [join(sep, "usr", "share", "bunkerweb", *paths) for paths in (("deps", "python"), ("utils",), ("db",))]:
    if deps_path not in sys_path:
        sys_path.append(deps_path)

from logger import setup_logger  # type: ignore
from jobs import Job  # type: ignore

LOGGER = setup_logger("LETS-ENCRYPT.renew", getenv("LOG_LEVEL", "INFO"))
LIB_PATH = Path(sep, "var", "lib", "bunkerweb", "letsencrypt")
CERTBOT_BIN = join(sep, "usr", "share", "bunkerweb", "deps", "python", "bin", "certbot")
DEPS_PATH = join(sep, "usr", "share", "bunkerweb", "deps", "python")

LOGGER_CERTBOT = setup_logger("LETS-ENCRYPT.renew.certbot", getenv("LOG_LEVEL", "INFO"))
status = 0

CACHE_PATH = Path(sep, "var", "cache", "bunkerweb", "letsencrypt")
DATA_PATH = CACHE_PATH.joinpath("etc")
WORK_DIR = join(sep, "var", "lib", "bunkerweb", "letsencrypt")
LOGS_DIR = join(sep, "var", "log", "bunkerweb", "letsencrypt")

try:
    # Check if we're using let's encrypt
    use_letsencrypt = False

    if getenv("MULTISITE", "no") == "no":
        use_letsencrypt = getenv("AUTO_LETS_ENCRYPT", "no") == "yes"
    else:
        for first_server in getenv("SERVER_NAME", "").split(" "):
            if first_server and getenv(f"{first_server}_AUTO_LETS_ENCRYPT", "no") == "yes":
                use_letsencrypt = True
                break

    if not use_letsencrypt:
        LOGGER.info("Let's Encrypt is not activated, skipping renew...")
        sys_exit(0)

    JOB = Job(LOGGER, __file__)

    process = Popen(
        [
            CERTBOT_BIN,
            "renew",
            "--no-random-sleep-on-renew",
            "--config-dir",
            DATA_PATH.as_posix(),
            "--work-dir",
            WORK_DIR,
            "--logs-dir",
            LOGS_DIR,
        ],
        stdin=DEVNULL,
        stderr=PIPE,
        universal_newlines=True,
        env=environ | {"PYTHONPATH": DEPS_PATH},
    )
    while process.poll() is None:
        if process.stderr:
            for line in process.stderr:
                LOGGER_CERTBOT.info(line.strip())

    if process.returncode != 0:
        status = 2
        LOGGER.error("Certificates renewal failed")

    # Save Let's Encrypt data to db cache
    if DATA_PATH.is_dir() and list(DATA_PATH.iterdir()):
        cached, err = JOB.cache_dir(DATA_PATH)
        if not cached:
            LOGGER.error(f"Error while saving Let's Encrypt data to db cache : {err}")
        else:
            LOGGER.info("Successfully saved Let's Encrypt data to db cache")
except SystemExit as e:
    status = e.code
except BaseException as e:
    status = 2
    LOGGER.error(f"Exception while running certbot-renew.py :\n{e}")

sys_exit(status)
