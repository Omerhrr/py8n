#!/usr/bin/env python3
"""Double-fork daemonizer for the Py8n backend — survives tool-call session teardown."""
import os
import sys
import time

LOG = "/tmp/py8n-backend.log"


def daemonize() -> None:
    if os.fork() > 0:
        sys.exit(0)  # parent exits
    os.setsid()  # new session, escape controlling terminal
    if os.fork() > 0:
        sys.exit(0)  # first child exits
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "rb") as devnull:
        os.dup2(devnull.fileno(), 0)
    log = open(LOG, "ab", buffering=0)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)


if __name__ == "__main__":
    os.chdir("/home/z/my-project/mini-services/api-backend")
    daemonize()
    os.execv(
        "/home/z/.venv/bin/python",
        [
            "/home/z/.venv/bin/python",
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--log-level",
            "info",
        ],
    )
