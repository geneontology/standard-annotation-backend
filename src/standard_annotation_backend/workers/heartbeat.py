"""Write the heartbeats that worker and scheduler health checks read.

`workers/health.py` checks how old each heartbeat is. The worker writes its
heartbeat from a timer in Celery's main process while it is connected to Redis, and
Beat writes its heartbeat on each scheduling pass, so either stops when its main
loop stops.
"""

from typing import Any

from celery import bootsteps
from celery.beat import PersistentScheduler

from standard_annotation_backend.workers.health import (
    HEARTBEAT_INTERVAL_SECONDS,
    Process,
    write_heartbeat,
)


class WorkerHeartbeat(bootsteps.StartStopStep):
    """Write the worker's heartbeat now and every `HEARTBEAT_INTERVAL_SECONDS`.

    A step of the worker's consumer, the part of Celery that holds the Redis
    connection (Celery's "Extensions and Bootsteps" guide). Celery restarts the
    consumer's steps after a lost connection, and its restart replaces the timer
    entries scheduled before it, so a step of the worker itself would stop beating
    for good after the first reconnect. As a consumer step, the heartbeat stops
    while the worker is disconnected and starts again once it reconnects.

    The timer runs in the worker's main process, not in the child processes that
    run jobs, so a long job does not delay it.

    The interval is short because of how kombu's timer cancels an entry: it marks
    the entry cancelled and leaves it scheduled until its due time. While the
    consumer restarts, the event loop can sleep until that time before anything
    else runs, including the step that lets the worker take jobs again. A
    30-second interval held a reconnected worker idle for up to 30 seconds; two
    seconds, the interval of Celery's own event heartbeat, keeps it brief.
    """

    requires = frozenset({"celery.worker.consumer.connection:Connection"})

    def __init__(self, parent: Any, **kwargs: Any) -> None:
        super().__init__(parent, **kwargs)
        self.timer_entry: Any = None

    def start(self, parent: Any) -> None:
        """Write a heartbeat and schedule the rest on the consumer's timer."""
        write_heartbeat(Process.WORKER)
        self.timer_entry = parent.timer.call_repeatedly(
            HEARTBEAT_INTERVAL_SECONDS, write_heartbeat, (Process.WORKER,)
        )

    def stop(self, parent: Any) -> None:
        """Stop writing heartbeats."""
        if self.timer_entry is not None:
            self.timer_entry.cancel()
            self.timer_entry = None


class HeartbeatScheduler(PersistentScheduler):
    """Beat's default scheduler, writing the scheduler's heartbeat on each pass.

    Beat runs a pass at least every `beat_max_loop_interval` seconds, which
    `workers/celery_app.py` keeps well below the health check's maximum age.
    """

    def tick(self, *args: Any, **kwargs: Any) -> float:
        """Run one scheduling pass, then record that the scheduler is running."""
        interval = super().tick(*args, **kwargs)
        write_heartbeat(Process.SCHEDULER)
        return interval
