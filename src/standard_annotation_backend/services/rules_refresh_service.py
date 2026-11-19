"""Run `rules` refresh jobs for `RefreshRunner` from six go-site metadata files.

The fetcher reads the six files from one go-site commit and combines them into
one source document with `pack_source_files`. All six are validated before
anything is stored. The new snapshot (one stored version of the rule data), its
list of retracted publications, and its audit event are committed in one
transaction, so the six files always become active together. A file that is
invalid or cannot be decoded fails the job and leaves the previous snapshot
active. A refresh stores nothing until the six files are valid, and stores the
snapshot in the job's transaction.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from standard_annotation_backend.domain.jobs import (
    JobFailureCode,
    JobType,
    ProgressReporter,
)
from standard_annotation_backend.domain.ontology import OntologyCatalog
from standard_annotation_backend.domain.refresh import (
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
from standard_annotation_backend.domain.snapshots import SnapshotKind, SourceContent
from standard_annotation_backend.domain.stored_json import StoredJson
from standard_annotation_backend.go_site.rule_files import (
    InvalidRuleFileError,
    RuleFiles,
    parse_rule_files,
)
from standard_annotation_backend.persistence.models import ReferenceSnapshotRecord
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from standard_annotation_backend.refresh.decoding import decode_source_text
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
        refreshed_at: When the snapshot was activated.
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
"""The stored form of a rules refresh job's result."""


class RulesRefreshService:
    """Validate all six go-site files before activating them as one snapshot.

    Args:
        ontologies: The configured ontologies. An `extensions-constraints.yaml`
            namespace that one of them owns as a prefix produces no warning.
    """

    def __init__(self, ontologies: OntologyCatalog) -> None:
        self._ontologies = ontologies

    @property
    def name(self) -> RefreshKindName:
        """Return `rules`."""
        return RefreshKindName.RULES

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `rules_refresh`."""
        return frozenset({JobType.RULES_REFRESH})

    def apply(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        job: Job,
        document: SourceDocument,
        report: ProgressReporter,
    ) -> RefreshOutcome:
        """Store and activate the six files as one snapshot unless already active.

        Files whose content the active snapshot already holds, read with the same
        reader version, are not stored again, even when they come from a newer
        commit. Either way, rules snapshots that are no longer kept are pruned.

        Raises:
            SourceError: With `invalid_gzip` or `invalid_utf8` if a file cannot
                be decoded.
            TerminalRefreshError: With `invalid_document` if the content was not
                written by `pack_source_files`, or a file is missing or invalid.
        """
        snapshots = unit_of_work.snapshots
        content = SourceContent.of(RefreshKindName.RULES, document.provenance)
        active = snapshots.matching_active(
            SnapshotKind.RULES, document.source_key, content
        )
        if active is not None:
            outcome = self._outcome(unit_of_work, active, unchanged=True)
        else:
            parsed = _parse(document)
            report("storing")
            record = snapshots.create(
                SnapshotKind.RULES, document.source_key, job.job_id, document.provenance
            )
            unit_of_work.rules.insert(
                record.snapshot_id,
                STORED_RULE_DATA.dump(parsed.data),
                parsed.retracted_pmids,
            )
            report("activating")
            snapshots.activate(record)
            outcome = self._outcome(unit_of_work, record, unchanged=False)
            AuditService(unit_of_work.audit).record_rules_refreshed(
                actor_id=job.requested_by,
                job_id=job.job_id,
                details=outcome.result,
            )
        snapshots.prune(SnapshotKind.RULES)
        return outcome

    def _outcome(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        record: ReferenceSnapshotRecord,
        *,
        unchanged: bool,
    ) -> RefreshOutcome:
        """Build the job's outcome from an active rules snapshot.

        Warnings about unrecognized extension namespaces use the ontologies
        configured now, so adding an ontology removes the warning for its prefix.

        Raises:
            StoredDataError: If the stored rule data is invalid.
        """
        if record.activated_at is None:
            raise RuntimeError("a rules snapshot was read before it was activated")
        data = unit_of_work.snapshots.rule_data(
            record.snapshot_id, record.reader_version
        ).data
        namespaces = unrecognized_extension_namespaces(data, self._ontologies.prefixes)
        result = RulesRefreshResult(
            snapshot_id=record.snapshot_id,
            source_type=record.source_type,
            source_locator=record.source_locator,
            source_revision=record.source_revision,
            source_checksum=record.source_checksum,
            fetched_at=record.fetched_at,
            refreshed_at=record.activated_at,
            database_count=len(data.databases),
            go_ref_count=len(data.go_refs),
            group_count=len(data.group_shorthands),
            retracted_publication_count=(
                unit_of_work.rules.retracted_publication_count(record.snapshot_id)
            ),
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


def _parse(document: SourceDocument) -> RuleFiles:
    """Unpack, decode, and validate the six go-site files.

    Raises:
        SourceError: With `invalid_gzip` or `invalid_utf8`.
        TerminalRefreshError: With `invalid_document`.
    """
    try:
        files = unpack_source_files(document.content)
    except ValueError:
        raise TerminalRefreshError(
            failure_code=JobFailureCode.INVALID_DOCUMENT
        ) from None
    texts = {path: decode_source_text(content) for path, content in files.items()}
    try:
        return parse_rule_files(texts)
    except InvalidRuleFileError as error:
        raise TerminalRefreshError(
            failure_code=JobFailureCode.INVALID_DOCUMENT,
            failure_details=validation_failure_details(error.issues),
        ) from None
