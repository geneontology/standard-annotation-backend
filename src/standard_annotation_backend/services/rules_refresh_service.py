"""Run `rules` refresh jobs for `RefreshRunner` from six go-site metadata files.

The fetcher reads the six files from one go-site commit and combines them into
one source document with `pack_source_files`. All six are validated before
anything is stored. The new snapshot (one stored version of the rule data), its
list of retracted publications, and its audit event are committed in one
transaction, so the six files always become active together. A file that is
invalid or cannot be decoded fails the job and leaves the previous snapshot
active. Unlike some other kinds, a rules refresh writes no intermediate staging
rows. If Celery delivers the same job again after an interruption, the job
finds the snapshot it already stored by its job ID.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import (
    ProgressReporter,
    RefreshFailureCode,
    RefreshKindName,
    RefreshOutcome,
    SourceDocument,
    TerminalRefreshError,
    unpack_source_files,
    validation_failure_details,
)
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    STORED_RULE_DATA,
    unrecognized_extension_namespaces,
)
from standard_annotation_backend.domain.stored_json import StoredJson
from standard_annotation_backend.go_site.rule_files import (
    InvalidRuleFileError,
    parse_rule_files,
)
from standard_annotation_backend.persistence.models import RulesSnapshotRecord
from standard_annotation_backend.persistence.repositories.rules import same_rule_files
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.refresh.decoding import decode_source_text
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job


@dataclass(frozen=True, slots=True)
class RulesRefreshResult:
    """Describe the active GO Rule data snapshot after a refresh.

    Attributes:
        snapshot_id: The active snapshot.
        source_type: `github`.
        source_locator: `github:<repository>`.
        source_revision: The commit the files were read from.
        source_checksum: SHA-256 checksum of the combined source document.
        fetched_at: When the files were fetched.
        refreshed_at: When the snapshot was stored.
        database_count: Databases in `db-xrefs.yaml`.
        go_ref_count: References in `gorefs.yaml`.
        group_count: Groups in `groups.yaml`.
        retracted_publication_count: PMIDs in `retracted-publications.txt`.
        eco_usage_constraint_count: Entries in `eco-usage-constraints.yaml`.
        extension_constraint_count: Entries in `extensions-constraints.yaml`.
        unrecognized_extension_namespaces: `extensions-constraints.yaml`
            namespaces that are neither a configured ontology prefix nor a
            `db-xrefs.yaml` database, checked against the ontologies configured
            when this result was built.
    """

    snapshot_id: UUID
    source_type: str
    source_locator: str
    source_revision: str | None
    source_checksum: str
    fetched_at: datetime
    refreshed_at: datetime
    database_count: int
    go_ref_count: int
    group_count: int
    retracted_publication_count: int
    eco_usage_constraint_count: int
    extension_constraint_count: int
    unrecognized_extension_namespaces: tuple[str, ...]


STORED_RULES_REFRESH_RESULT = StoredJson(
    RulesRefreshResult, label="rules refresh result"
)


class RulesRefreshService:
    """Validate all six go-site files before activating them as one snapshot.

    Args:
        unit_of_work_factory: Opens the transactions for snapshots and audit.
        sources: Configured sources. An `extensions-constraints.yaml` namespace
            that matches one of their ontology prefixes produces no warning.
    """

    def __init__(
        self, unit_of_work_factory: UnitOfWorkFactory, sources: RefreshSources
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._sources = sources

    @property
    def name(self) -> RefreshKindName:
        """Return `rules`."""
        return RefreshKindName.RULES

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `rules_refresh`."""
        return frozenset({JobType.RULES_REFRESH})

    def recover(self, job: Job) -> RefreshOutcome | None:
        """Return the outcome of the snapshot this job stored, if it still exists."""
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.rules.get_by_job(job.job_id)
            if record is None:
                return None
            return self._outcome(record, unchanged=False)

    def apply(
        self, job: Job, document: SourceDocument, report: ProgressReporter
    ) -> RefreshOutcome:
        """Validate the six files, then store and activate them as one snapshot.

        Files identical to those of the active snapshot are not stored again, even
        when they come from a newer commit, provided the snapshot already holds
        the data they parse to. The files are always parsed before this check, so
        after an upgrade to a SABE release that parses them differently, the
        first refresh stores them again.

        Raises:
            SourceError: With `invalid_gzip` or `invalid_utf8` if a file cannot be
                decoded.
            TerminalRefreshError: With `invalid_document` if the content was not
                written by `pack_source_files`, or a file is missing or invalid.
        """
        try:
            files = unpack_source_files(document.content)
        except ValueError:
            raise TerminalRefreshError(
                failure_code=RefreshFailureCode.INVALID_DOCUMENT
            ) from None
        texts = {path: decode_source_text(content) for path, content in files.items()}
        try:
            parsed = parse_rule_files(texts)
        except InvalidRuleFileError as error:
            raise TerminalRefreshError(
                failure_code=RefreshFailureCode.INVALID_DOCUMENT,
                failure_details=validation_failure_details(error.issues),
            ) from None
        rule_data = STORED_RULE_DATA.dump(parsed.data)
        with self._unit_of_work_factory() as unit_of_work:
            active = unit_of_work.rules.get_active()
            if (
                active is not None
                and same_rule_files(active, document.provenance)
                and unit_of_work.rules.stores(active, rule_data, parsed.retracted_pmids)
            ):
                return self._outcome(active, unchanged=True)
        with self._unit_of_work_factory() as unit_of_work:
            activation = unit_of_work.rules.activate(
                job_id=job.job_id,
                provenance=document.provenance,
                rule_data=rule_data,
                retracted_pmids=parsed.retracted_pmids,
            )
            # Another job may have activated the same files first.
            outcome = self._outcome(activation.record, unchanged=not activation.applied)
            if activation.applied:
                AuditService(unit_of_work.audit).record_rules_refreshed(
                    actor_id=job.requested_by,
                    job_id=job.job_id,
                    details=outcome.result,
                )
            unit_of_work.commit()
        return outcome

    def discard_staging(self, uow: SqlAlchemyUnitOfWork, job_id: UUID) -> None:
        """Do nothing; rules refreshes write no staging rows."""

    def after_terminal(self, source_key: str) -> None:
        """Do nothing; rules refreshes have no follow-up work."""

    def _outcome(
        self, record: RulesSnapshotRecord, *, unchanged: bool
    ) -> RefreshOutcome:
        """Build the job's outcome from a stored snapshot.

        Warnings about unrecognized extension namespaces use the ontologies
        configured now, so adding an ontology removes the warning for its prefix.

        Raises:
            StoredDataError: If the stored rule data is invalid.
        """
        data = STORED_RULE_DATA.load(record.rule_data)
        namespaces = unrecognized_extension_namespaces(data, self._ontology_prefixes())
        result = RulesRefreshResult(
            snapshot_id=record.snapshot_id,
            source_type=record.source_type,
            source_locator=record.source_locator,
            source_revision=record.source_revision,
            source_checksum=record.source_checksum,
            fetched_at=record.fetched_at,
            refreshed_at=record.refreshed_at,
            database_count=len(data.databases),
            go_ref_count=len(data.go_refs),
            group_count=len(data.group_shorthands),
            retracted_publication_count=record.retracted_publication_count,
            eco_usage_constraint_count=len(data.eco_usage_constraints),
            extension_constraint_count=len(data.extension_constraints),
            unrecognized_extension_namespaces=namespaces,
        )
        return RefreshOutcome(
            result=STORED_RULES_REFRESH_RESULT.dump(result),
            counts={
                "database_count": result.database_count,
                "go_ref_count": result.go_ref_count,
                "group_count": result.group_count,
                "retracted_publication_count": result.retracted_publication_count,
                "eco_usage_constraint_count": result.eco_usage_constraint_count,
                "extension_constraint_count": result.extension_constraint_count,
                "unrecognized_extension_namespace_count": len(namespaces),
            },
            warnings=tuple(
                f"extensions-constraints.yaml namespace {namespace} is neither a "
                "configured ontology prefix nor a db-xrefs.yaml database"
                for namespace in namespaces
            ),
            unchanged=unchanged,
        )

    def _ontology_prefixes(self) -> frozenset[str]:
        """Return every identifier prefix owned by a configured ontology."""
        return frozenset(
            prefix
            for key in self._sources.keys(RefreshKindName.ONTOLOGY)
            for prefix in self._sources.ontology_definition(key).identifier_prefixes
        )
