"""Start a SABE Celery worker that returns its messages when it is stopped.

Run it as `python -m standard_annotation_backend.workers.worker`, followed by any
`celery worker` options. Container platforms such as ECS stop a container with
SIGTERM, then kill it after a stop timeout. Celery treats SIGTERM as a warm shutdown
and waits for running jobs however long they take, so a long job is killed with its
message still unacknowledged, and Redis does not redeliver it until its visibility
timeout passes.

Setting `REMAP_SIGTERM=SIGQUIT` makes SIGTERM start Celery's cold shutdown instead:
the worker lets running jobs finish for `worker_soft_shutdown_timeout` seconds
(`SABE_WORKER_SOFT_SHUTDOWN_SECONDS`), then cancels them and puts their messages back
on the queue for another worker. Celery reads the variable while it is being
imported, so this command sets it and then replaces itself with the Celery command.
The replacement keeps the process ID, so the platform's SIGTERM still reaches Celery.
"""

import os
import sys
from collections.abc import Sequence
from typing import NoReturn

CELERY_APP = "standard_annotation_backend.workers.celery_app:celery_app"


def main(argv: Sequence[str] | None = None) -> NoReturn:
    """Replace this process with a Celery worker for SABE's app.

    Args:
        argv: `celery worker` options; the command line's arguments if omitted.
    """
    options = sys.argv[1:] if argv is None else list(argv)
    os.environ["REMAP_SIGTERM"] = "SIGQUIT"
    command = [sys.executable, "-m", "celery", "-A", CELERY_APP, "worker", *options]
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
