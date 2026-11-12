"""Run reference data refreshes from the command line.

`just refresh KIND [SOURCE]` runs this module. It starts jobs through the same
`RefreshStartService` as the admin API and the schedule, so jobs and audit events
are recorded the same way. Instead of sending each job to a Celery worker, it
runs the job in this process and waits for it. This is the supported way to load
authorization into a new database, before anyone holds a token for the admin API.
Annotation cutovers are available only through the admin API. Term replacement jobs
queued by the refreshes run in this process afterward, once each.
"""

import argparse
import logging
import sys
from collections.abc import Sequence

from pydantic import ValidationError
from pydantic_settings import SettingsError
from sqlalchemy.exc import SQLAlchemyError

from standard_annotation_backend.domain.annotation_management import (
    GroupSabeManagedError,
)
from standard_annotation_backend.domain.auth import system_context
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import (
    RefreshKindName,
    UnknownSourceError,
)
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.refresh_start_service import (
    RefreshStartService,
)
from standard_annotation_backend.workers.runtime import WorkerRuntime, worker_runtime

logger = logging.getLogger(__name__)

_SINGLE_SOURCE_KINDS = frozenset({RefreshKindName.AUTHORIZATION, RefreshKindName.RULES})
"""Kinds with exactly one configured source, which take no `source_key`."""


def run_refreshes(
    runtime: WorkerRuntime, kind: RefreshKindName, source_key: str | None
) -> tuple[Job, ...]:
    """Start refreshes of one kind and run each job in this process.

    Args:
        runtime: Services, runner, and settings for this process.
        kind: Kind of reference data to refresh.
        source_key: One source to refresh, or `None` for every configured source.

    Returns:
        The state of every started job after it ran.

    Raises:
        UnknownSourceError: If `source_key` is not configured for `kind`.
    """

    def run_in_process(job: Job) -> None:
        # The start service calls this after committing the jobs, where the API
        # and the schedule would send the job to Celery instead.
        try:
            if job.job_type is JobType.ENTITY_RETIREMENT:
                runtime.runner.run_retirement(job.job_id)
            else:
                runtime.runner.run(job.job_id)
        except Exception as error:
            # A Celery worker would retry this job later. The CLI does not wait to
            # retry: the job stays running with whatever work it committed, the
            # summary below reports it, and the next start for the same source
            # reuses the job and resumes it.
            logger.error(
                "Refresh job stopped before finishing: job_id=%s failure_type=%s",
                job.job_id,
                type(error).__name__,
            )

    started = RefreshStartService(
        runtime.unit_of_work_factory, runtime.settings.sources, run_in_process
    ).start(kind, context=system_context("cli"), source_key=source_key)
    # `start` returns each job as it was before it ran; read the outcome.
    return tuple(runtime.jobs.find(job.job_id) for job in started)


def run_replacements(runtime: WorkerRuntime, queued: Sequence[Job]) -> tuple[Job, ...]:
    """Run each queued term replacement job once, in this process.

    Activations and publications queue these jobs; a worker would run them
    from Celery. Several triggers can share one job, so each is run once.

    Returns:
        The state of every replacement job after it ran.
    """
    ran: list[Job] = []
    for job_id in dict.fromkeys(job.job_id for job in queued):
        try:
            runtime.term_replacement.run(job_id)
        except Exception as error:
            logger.error(
                "Term replacement job stopped before finishing: job_id=%s failure_type=%s",
                job_id,
                type(error).__name__,
            )
        ran.append(runtime.jobs.find(job_id))
    return tuple(ran)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run the refreshes, and print one line per job.

    Returns:
        Zero when every job succeeded or there was nothing to refresh, otherwise
        one. Argument errors exit with status two.
    """
    parser = argparse.ArgumentParser(
        prog="refresh",
        description="Refresh SABE reference data from config/sources.yaml.",
    )
    parser.add_argument("kind", choices=[kind.value for kind in RefreshKindName])
    parser.add_argument(
        "source_key",
        nargs="?",
        help=(
            "Refresh one ontology, entity, or annotation source. Omit it to refresh "
            "every configured source. Authorization and rules each have a single "
            "source and take none."
        ),
    )
    arguments = parser.parse_args(argv)
    kind = RefreshKindName(arguments.kind)
    if kind in _SINGLE_SOURCE_KINDS and arguments.source_key is not None:
        parser.error(f"{kind.value} has a single source; omit source_key")
    try:
        # Without `enqueue_prune`, ontology pruning also runs in this process.
        replacements: list[Job] = []
        with worker_runtime(dispatch_replacement=replacements.append) as runtime:
            jobs = run_refreshes(runtime, kind, arguments.source_key)
            jobs += run_replacements(runtime, replacements)
            sources_configured = bool(runtime.settings.sources.keys(kind))
    except UnknownSourceError:
        print(f"No {kind.value} source is configured with that key", file=sys.stderr)
        return 1
    except GroupSabeManagedError:
        print(
            "That annotation source's group is SABE-managed; GPAD can no longer "
            "replace its annotations",
            file=sys.stderr,
        )
        return 1
    except (ValidationError, SettingsError):
        print("SABE configuration is invalid", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("Refresh could not record its jobs in PostgreSQL", file=sys.stderr)
        return 1
    if not jobs:
        # That is not a failure, but say why instead of printing nothing.
        if sources_configured:
            # Only annotation refreshes can start nothing while sources exist:
            # every source's group is SABE-managed.
            print(
                f"No {kind.value} sources need refreshing; their groups are SABE-managed"
            )
        else:
            # Nothing is configured for this kind (and, for entities, nothing needs
            # retiring).
            print(f"No {kind.value} sources are configured")
        return 0
    for job in jobs:
        print(_describe(job))
    return 0 if all(job.status is JobStatus.SUCCEEDED for job in jobs) else 1


def _describe(job: Job) -> str:
    """Return `<job type> <source key or scope> <status> <job id> [failure code]`."""
    scope = job.parameters.get("source_key")
    if job.job_type is JobType.TERM_REPLACEMENT:
        scope = job.parameters.get("group_key", "all")
    parts = [
        job.job_type.value,
        str(scope),
        job.status.value,
        str(job.job_id),
    ]
    failure_code = job.progress.get("failure_code")
    if job.status is JobStatus.FAILED and isinstance(failure_code, str):
        parts.append(failure_code)
    return " ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
