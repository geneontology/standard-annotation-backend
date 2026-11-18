"""Create or reuse refresh jobs and send them to be run.

The API, the Celery Beat schedule, and the refresh CLI all start refreshes
through `RefreshStartService`, so every path reuses existing work the same way.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from standard_annotation_backend.domain.annotation_management import (
    AnnotationJobInProgressError,
    GroupSabeManagedError,
)
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.auth import (
    AuthorizationContext,
    PermissionAction,
    authorize_role,
)
from standard_annotation_backend.domain.jobs import (
    ACTIVE_JOB_STATUSES,
    JobType,
)
from standard_annotation_backend.domain.refresh import (
    REFRESH_JOB_TYPES,
    RefreshKindName,
)
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import (
    Job,
    JobService,
    job_from_record,
)


@dataclass(frozen=True, slots=True)
class _StartedJob:
    """Pair a started job with whether this call created it."""

    job: Job
    created: bool


@dataclass(frozen=True, slots=True)
class RefreshStart:
    """Describe what one call to `RefreshStartService.start` did.

    Attributes:
        jobs: The created or reused refresh jobs, in source-key order.
        retired: The source keys whose active snapshots were retired because the
            sources file no longer lists them, in source-key order.
    """

    jobs: tuple[Job, ...]
    retired: tuple[str, ...] = ()


_RETIRED_KINDS: Mapping[RefreshKindName, SnapshotKind] = {
    RefreshKindName.ONTOLOGY: SnapshotKind.ONTOLOGY,
    RefreshKindName.ENTITY: SnapshotKind.ENTITY,
}
"""Kinds whose sources can be removed from the configuration and retired."""


class RefreshStartService:
    """Start refreshes of one kind for one source or for all configured sources.

    Args:
        unit_of_work_factory: Opens the transaction that creates jobs.
        sources: Configured sources for every kind.
        dispatch: Sends one job to be run. Celery callers enqueue a task; the
            CLI runs the job in its own process.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        sources: RefreshSources,
        dispatch: Callable[[Job], None],
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._sources = sources
        self._dispatch = dispatch

    def start(
        self,
        kind: RefreshKindName,
        *,
        context: AuthorizationContext,
        source_key: str | None,
    ) -> RefreshStart:
        """Create or reuse jobs, commit them, then dispatch each unfinished job.

        With `source_key`, one refresh job is started for that source. Without
        it, every configured source of the kind gets a refresh job, and for
        ontologies and entity catalogs, every source that still has an active
        snapshot but is no longer configured is retired in the same transaction.
        A source with an unfinished refresh job is left for a later start.
        Retired snapshots stop being read at once; the kind's next refresh job
        deletes their data. For annotation refreshes, groups whose annotations
        are SABE-managed or that have an unfinished cutover are skipped when
        `source_key` is omitted.

        If a queued or running job of the same type already exists for a source,
        that job is returned and dispatched again instead of creating another.
        If dispatching fails, only a job created by this call is marked failed,
        with failure code `dispatch_failed`.

        Args:
            kind: Kind of reference data to refresh.
            context: Caller's authorization; the caller is recorded as the
                jobs' requester.
            source_key: One source to refresh, or `None` for every source.

        Returns:
            The jobs started and the sources retired.

        Raises:
            PermissionDeniedError: If the caller lacks global admin. This is
                checked before the source lookup, so the caller learns nothing
                about which sources are configured.
            UnknownSourceError: If `source_key` is not configured for `kind`.
            GroupSabeManagedError: If `source_key` names an annotation source
                whose group is SABE-managed. No job is created.
            AnnotationJobInProgressError: If `source_key` names an annotation
                source whose group has an unfinished cutover. No job is created.
        """
        authorize_role(context, PermissionAction.REFRESH_CREATE)
        return self._start(
            kind, requested_by=context.actor_id, source_key=source_key, cutover=False
        )

    def start_cutover(self, *, context: AuthorizationContext, source_key: str) -> Job:
        """Create or reuse the cutover job for one annotation source and dispatch it.

        A cutover moves the source's group to SABE management permanently, so it
        is started only on request, never by the schedule or for every source.
        An unfinished cutover job for the source is reused, and an unfinished
        ordinary refresh for the source blocks the request (see
        `_annotation_targets`).

        Raises:
            PermissionDeniedError: If the caller lacks global admin, checked
                before the source lookup.
            UnknownSourceError: If `source_key` is not an annotation source.
            GroupSabeManagedError: If the group is already SABE-managed.
            AnnotationJobInProgressError: If the group has an unfinished ordinary
                refresh. No job is created.
        """
        authorize_role(context, PermissionAction.REFRESH_CREATE)
        (job,) = self._start(
            RefreshKindName.ANNOTATION,
            requested_by=context.actor_id,
            source_key=source_key,
            cutover=True,
        ).jobs
        return job

    def _start(
        self,
        kind: RefreshKindName,
        *,
        requested_by: str,
        source_key: str | None,
        cutover: bool,
    ) -> RefreshStart:
        """Create or reuse the jobs for `start` or `start_cutover`, then dispatch."""
        # Reject an unconfigured key before touching the database, so a typo
        # creates no job and no audit event.
        if source_key is not None:
            self._sources.source(kind, source_key)

        started: list[_StartedJob] = []
        with self._unit_of_work_factory() as uow:
            # Serialize job creation across every request, scheduler run, and
            # CLI invocation; see `JobRepository.lock_refresh_starts`. The lock
            # is held until this transaction commits, so the "find, else create"
            # below cannot interleave with another start.
            uow.jobs.lock_refresh_starts()
            retired = (
                self._retire_unconfigured(uow, kind, requested_by)
                if source_key is None
                else ()
            )
            for job_type, key in self._targets(uow, kind, source_key, cutover=cutover):
                # Reuse a queued or running job for the same source instead of
                # creating a duplicate. A running job may have lost its worker,
                # so it is dispatched again below and resumes.
                record = uow.jobs.find_active(job_type=job_type, source_key=key)
                created = record is None
                if record is None:
                    # New jobs store only the source key. Workers look the
                    # source up in the sources file when they run, so a job
                    # never carries a URL that the file no longer lists.
                    record = uow.jobs.create(
                        job_type=job_type,
                        requested_by=requested_by,
                        parameters={"source_key": key},
                        now=datetime.now(UTC),
                    )
                    AuditService(uow.audit).record_job_lifecycle(
                        action=AuditAction.JOB_QUEUED, record=record
                    )
                started.append(_StartedJob(job_from_record(record), created))
            # Commit before dispatching: a worker that receives the message must
            # be able to read the job. Dispatching first could deliver a job ID
            # that does not exist yet.
            uow.commit()
        return RefreshStart(
            jobs=tuple(self._dispatch_started(item) for item in started),
            retired=retired,
        )

    def _retire_unconfigured(
        self, uow: SqlAlchemyUnitOfWork, kind: RefreshKindName, actor_id: str
    ) -> tuple[str, ...]:
        """Retire the active snapshots of the kind's sources that are not configured.

        Only ontologies and entity catalogs can lose a source. A source with an
        unfinished refresh job is skipped: that job fails with `unknown_source`
        when it runs, and a later start retires the source. Each retirement is
        audited with the requester as the actor.

        Returns:
            The retired source keys, in order.
        """
        snapshot_kind = _RETIRED_KINDS.get(kind)
        if snapshot_kind is None:
            return ()
        configured = set(self._sources.keys(kind))
        refresh_type = REFRESH_JOB_TYPES[kind]
        removed = [
            key
            for key in uow.snapshots.active_source_keys(snapshot_kind)
            if key not in configured
            and uow.jobs.find_active(job_type=refresh_type, source_key=key) is None
        ]
        retired = uow.snapshots.retire(snapshot_kind, removed)
        audit = AuditService(uow.audit)
        for record in retired:
            audit.record_snapshot_retired(
                kind=snapshot_kind,
                actor_id=actor_id,
                details={
                    "source_key": record.source_key,
                    "snapshot_id": str(record.snapshot_id),
                },
            )
        return tuple(record.source_key for record in retired)

    def _targets(
        self,
        uow: SqlAlchemyUnitOfWork,
        kind: RefreshKindName,
        source_key: str | None,
        *,
        cutover: bool,
    ) -> list[tuple[JobType, str]]:
        """List the job type and source key of every job to start."""
        if kind is RefreshKindName.ANNOTATION:
            return self._annotation_targets(uow, source_key, cutover=cutover)
        refresh_type = REFRESH_JOB_TYPES[kind]
        if source_key is not None:
            return [(refresh_type, source_key)]
        return [(refresh_type, key) for key in self._sources.keys(kind)]

    def _annotation_targets(
        self, uow: SqlAlchemyUnitOfWork, source_key: str | None, *, cutover: bool
    ) -> list[tuple[JobType, str]]:
        """List annotation jobs to start, leaving out groups that cannot take one.

        A group whose annotations are SABE-managed never gets a GPAD job. Each
        source has at most one unfinished GPAD job: an unfinished job of the
        requested type is reused by `_start`, but one of the other type blocks
        the request. Modes and unfinished jobs are read while the start lock is
        held, so they match the jobs created in the same transaction.
        Publication checks the group's mode again, so a group that is cut over
        after this check still cannot be overwritten.

        Unfinished jobs are matched by source key, not by group, which is enough
        to keep one GPAD job per group. A job looks up its source when it runs,
        so a job left under a key the configuration no longer lists, such as
        after a source is renamed, fails with `unknown_source` before fetching
        and never touches the group. Every process restarts together when the
        configuration changes, so a job still running under the old key cannot
        overlap a new job under the new one.

        Raises:
            GroupSabeManagedError: If `source_key` names a source whose group is
                SABE-managed.
            AnnotationJobInProgressError: If `source_key` names a source whose
                group has an unfinished GPAD job of the other type.
        """
        job_type = JobType.ANNOTATION_CUTOVER if cutover else JobType.ANNOTATION_REFRESH
        other_type = (
            JobType.ANNOTATION_REFRESH if cutover else JobType.ANNOTATION_CUTOVER
        )
        managed = uow.annotation_imports.sabe_managed_groups()
        if source_key is not None:
            group = self._sources.annotation_group(source_key)
            if group in managed:
                raise GroupSabeManagedError(group)
            if (
                uow.jobs.find_active(job_type=other_type, source_key=source_key)
                is not None
            ):
                raise AnnotationJobInProgressError
            return [(job_type, source_key)]
        return [
            (job_type, key)
            for key in self._sources.keys(RefreshKindName.ANNOTATION)
            if self._sources.annotation_group(key) not in managed
            and uow.jobs.find_active(job_type=other_type, source_key=key) is None
        ]

    def _dispatch_started(self, item: _StartedJob) -> Job:
        """Send an unfinished job through `JobService.send`.

        If sending fails, the job is failed with `dispatch_failed` only when this
        call created it and no worker has started it.
        """
        # `find_active` only returns queued or running jobs, and new jobs are
        # queued, so this guard is defensive.
        if item.job.status not in ACTIVE_JOB_STATUSES:
            return item.job
        return JobService(self._unit_of_work_factory).send(
            item.job, created=item.created, dispatch=self._dispatch
        )
