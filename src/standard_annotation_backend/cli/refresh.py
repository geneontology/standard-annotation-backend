"""Run reference data refreshes from the command line.

`just refresh KIND [SOURCE]` runs this module. It starts jobs through the same
`RefreshStartService` as the admin API and the schedule, so jobs and audit events
are recorded the same way. Instead of sending each job to a Celery worker, it
runs the jobs in this process, one at a time, after they are sent, and waits for
them. This is the supported way to load authorization into a new database,
before anyone holds a token for the admin API: `--if-never-loaded` refreshes
authorization only if none has ever been applied, so a deploy can run it every
time. Annotation cutovers are available
only through the admin API. After the refreshes, any term replacement jobs they
queued also run in this process. One whose groups another job of its type is
already covering is deferred: it stays queued until the next ontology activation
or annotation publication sends it.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from uuid import UUID

from pydantic import ValidationError
from pydantic_settings import SettingsError
from sqlalchemy.exc import SQLAlchemyError

from standard_annotation_backend.domain.annotation_management import (
    AnnotationJobInProgressError,
    GroupSabeManagedError,
)
from standard_annotation_backend.domain.auth import system_context
from standard_annotation_backend.domain.jobs import JobStatus
from standard_annotation_backend.domain.refresh import (
    RefreshKindName,
    UnknownSourceError,
)
from standard_annotation_backend.services.annotation_scan import (
    AnnotationScanBusyError,
)
from standard_annotation_backend.services.authorization_refresh_service import (
    authorization_loaded,
)
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.refresh_start_service import (
    RefreshStartService,
)
from standard_annotation_backend.workers.runtime import WorkerRuntime, worker_runtime

logger = logging.getLogger(__name__)

_DEFERRED = (
    " deferred: another job of this type and scope is running; the next ontology "
    "activation or annotation publication sends it"
)
"""Printed after a follow-up job that stayed queued because its scope was busy."""

_SINGLE_SOURCE_KINDS = frozenset({RefreshKindName.AUTHORIZATION, RefreshKindName.RULES})
"""Kinds with exactly one configured source, which take no `source_key`."""


def run_refreshes(
    runtime: WorkerRuntime, kind: RefreshKindName, source_key: str | None
) -> tuple[tuple[Job, ...], tuple[str, ...]]:
    """Start refreshes of one kind and run each job in this process.

    Each job runs after `RefreshStartService.start` returns, not while it is
    being sent: `JobService.send` holds the job's row lock while it sends, and a
    job run then would wait on that lock forever.

    Args:
        runtime: Services, runner, and settings for this process.
        kind: Kind of reference data to refresh.
        source_key: One source to refresh, or `None` for every configured source.

    Returns:
        The state of every started job after it ran, and the keys of the sources
        that starting the refreshes retired.

    Raises:
        UnknownSourceError: If `source_key` is not configured for `kind`.
    """
    # The start service sends each job here after committing it, where the API
    # and the schedule would send it to Celery instead.
    sent: list[Job] = []
    started = RefreshStartService(
        runtime.unit_of_work_factory, runtime.settings.sources, sent.append
    ).start(kind, context=system_context("cli"), source_key=source_key)
    for job in sent:
        try:
            runtime.executors.run(job.job_id)
        except Exception as error:
            # A Celery worker would retry this job later. The CLI does not wait to
            # retry: the job stays running with nothing committed, the summary
            # below reports it, and the next start for the same source reuses
            # the job and runs it again.
            logger.error(
                "Refresh job stopped before finishing: job_id=%s failure_type=%s",
                job.job_id,
                type(error).__name__,
            )
    # `start` returns each job as it was before it ran; read the outcome.
    return (
        tuple(runtime.jobs.find(job.job_id) for job in started.jobs),
        started.retired,
    )


def run_follow_ups(
    runtime: WorkerRuntime, queued: Sequence[Job]
) -> tuple[tuple[Job, ...], frozenset[UUID]]:
    """Run each follow-up job once, in this process.

    Ontology activations and annotation publications queue term replacement
    jobs, which a Celery worker would otherwise run. Several refreshes can queue
    the same job, so each distinct job runs once. A job whose groups another job
    of its type is covering does not start: it stays queued until the next
    ontology activation or annotation publication sends it. A job that raises
    anything else is logged and reported in whatever state it reached.

    Returns:
        The state of every follow-up job after it ran, and the IDs of the jobs
        that were deferred because their scope was busy.
    """
    ran: list[Job] = []
    deferred: set[UUID] = set()
    for job_id in dict.fromkeys(job.job_id for job in queued):
        try:
            runtime.executors.run(job_id)
        except AnnotationScanBusyError:
            logger.info(
                "Follow-up job deferred: job_id=%s, another job of its type and "
                "scope is running",
                job_id,
            )
            deferred.add(job_id)
        except Exception as error:
            logger.error(
                "Follow-up job stopped before finishing: job_id=%s failure_type=%s",
                job_id,
                type(error).__name__,
            )
        ran.append(runtime.jobs.find(job_id))
    return tuple(ran), frozenset(deferred)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run the refreshes, and print a line per retirement and job.

    Refreshing every ontology or entity source also retires the sources that
    `config/sources.yaml` no longer lists; each is printed as
    `<kind> <source key> retired` before the job lines. A follow-up job deferred
    because another job covers its groups is printed as queued, followed by
    "deferred" and the reason.

    Returns:
        Zero when every job succeeded or was deferred, or there was nothing to
        refresh, otherwise one. Argument errors exit with status two.
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
            "every configured source; for ontologies and entities, this also "
            "retires the sources that config/sources.yaml no longer lists. "
            "Authorization and rules each have a single source and take none."
        ),
    )
    parser.add_argument(
        "--if-never-loaded",
        action="store_true",
        help=(
            "Refresh authorization only if no authorization refresh has ever been "
            "applied to this database, so a deploy can run it every time. Without "
            "authorization, nobody can sign in to use the admin refresh API."
        ),
    )
    arguments = parser.parse_args(argv)
    kind = RefreshKindName(arguments.kind)
    if kind in _SINGLE_SOURCE_KINDS and arguments.source_key is not None:
        parser.error(f"{kind.value} has a single source; omit source_key")
    if arguments.if_never_loaded and kind is not RefreshKindName.AUTHORIZATION:
        parser.error("--if-never-loaded applies only to authorization")
    try:
        follow_ups: list[Job] = []
        with worker_runtime(dispatch_follow_up=follow_ups.append) as runtime:
            if arguments.if_never_loaded and authorization_loaded(
                runtime.unit_of_work_factory
            ):
                print("Authorization is already loaded; not refreshing it")
                return 0
            jobs, retired = run_refreshes(runtime, kind, arguments.source_key)
            follow_up_jobs, deferred = run_follow_ups(runtime, follow_ups)
            jobs += follow_up_jobs
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
    except AnnotationJobInProgressError:
        # The CLI never starts cutovers, so the only conflict it meets is an
        # unfinished cutover.
        print(
            "That annotation source's group has an unfinished GPAD cutover job",
            file=sys.stderr,
        )
        return 1
    except (ValidationError, SettingsError):
        print("SABE configuration is invalid", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("Refresh could not record its jobs in PostgreSQL", file=sys.stderr)
        return 1
    if not jobs and not retired:
        # That is not a failure, but say why instead of printing nothing.
        if sources_configured:
            # Only annotation refreshes can start nothing while sources exist:
            # every source's group is either SABE-managed or has an unfinished
            # cutover.
            print(
                f"No {kind.value} sources need refreshing; their groups are "
                "SABE-managed or have an unfinished cutover"
            )
        else:
            # Nothing is configured for this kind.
            print(f"No {kind.value} sources are configured")
        return 0
    for key in retired:
        print(f"{kind.value} {key} retired")
    waiting = {
        job.job_id
        for job in jobs
        if job.job_id in deferred and job.status is JobStatus.QUEUED
    }
    for job in jobs:
        print(_describe(job) + (_DEFERRED if job.job_id in waiting else ""))
    finished = all(
        job.status is JobStatus.SUCCEEDED or job.job_id in waiting for job in jobs
    )
    return 0 if finished else 1


def _describe(job: Job) -> str:
    """Return `<job type> <source key or groups> <status> <job id> [failure code]`."""
    scope = job.parameters.get("source_key")
    if scope is None:
        # Annotation scan jobs name the group they cover, or none for all
        # groups, instead of a source. Reading the key directly never raises,
        # so a job with unexpected parameters is still printed.
        scope = job.parameters.get("group_key", "all")
    parts = [job.job_type.value, str(scope), job.status.value, str(job.job_id)]
    failure_code = job.progress.get("failure_code")
    if job.status is JobStatus.FAILED and isinstance(failure_code, str):
        parts.append(failure_code)
    return " ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
