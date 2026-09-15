"""Test annotation operations independently from HTTP and PostgreSQL."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import TracebackType
from typing import cast
from uuid import UUID

import pytest

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence.repositories import (
    AnnotationNotFoundError,
    AnnotationSearchFilters,
    Page,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_service import (
    AnnotationHistoryNotFoundError,
    AnnotationService,
    EmptyAnnotationPatchError,
    InvalidAnnotationPayloadError,
    RequestContext,
)

FIXED_ID = UUID("00000000-0000-0000-0000-000000000301")
FIXED_JOB_ID = UUID("00000000-0000-0000-0000-000000000399")
CREATED_AT = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
UPDATED_AT = datetime(2026, 9, 11, 10, 5, tzinfo=UTC)
REQUEST_CONTEXT = RequestContext(actor_id="provisional-api-user")


def _valid_payload() -> dict[str, object]:
    return {
        "db_object_id": "UniProtKB:P12345",
        "negation": None,
        "relation": "RO:0002331",
        "ontology_class_id": "GO:0008150",
        "references": ["PMID:1", "PMID:2"],
        "evidence_type": "ECO:0000314",
        "with_or_from": ["UniProtKB:Q1"],
        "interacting_taxon_id": ["NCBITaxon:9606"],
        "annotation_date": "2026-09-09",
        "assigned_by": "GO_Central",
        "annotation_extensions": [
            {
                "extension_relation": "BFO:0000050",
                "extension_term": "CL:0000000",
            }
        ],
        "annotation_properties": {"comment": ["reviewed"]},
    }


@dataclass(slots=True)
class CurrentRecord:
    annotation_id: UUID
    annotation_data: dict[str, object]
    current_version: int
    status: str
    deleted_at: datetime | None
    owning_group_id: str
    record_origin: str
    source_import_job_id: UUID | None
    duplicate_base_signature: str
    db_object_id: str
    negation: bool
    relation: str
    ontology_class_id: str
    evidence_type: str
    annotation_date: date
    assigned_by: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class VersionRecord:
    annotation_id: UUID
    version: int
    annotation_data: dict[str, object]
    is_deleted: bool
    actor_id: str
    change_source: str
    created_at: datetime


def _current_record(
    *,
    annotation_data: dict[str, object] | None = None,
    version: int = 1,
) -> CurrentRecord:
    payload = annotation_data or _valid_payload()
    return CurrentRecord(
        annotation_id=FIXED_ID,
        annotation_data=payload,
        current_version=version,
        status="active",
        deleted_at=None,
        owning_group_id="group-1",
        record_origin="direct",
        source_import_job_id=None,
        duplicate_base_signature="0" * 64,
        db_object_id=cast(str, payload["db_object_id"]),
        negation=bool(payload["negation"]),
        relation=cast(str, payload["relation"]),
        ontology_class_id=cast(str, payload["ontology_class_id"]),
        evidence_type=cast(str, payload["evidence_type"]),
        annotation_date=date(2026, 9, 9),
        assigned_by=cast(str, payload["assigned_by"]),
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


def _version_record(
    version: int,
    *,
    annotation_data: dict[str, object] | None = None,
    is_deleted: bool = False,
) -> VersionRecord:
    return VersionRecord(
        annotation_id=FIXED_ID,
        version=version,
        annotation_data=annotation_data or _valid_payload(),
        is_deleted=is_deleted,
        actor_id="provisional-api-user",
        change_source="api",
        created_at=UPDATED_AT,
    )


class FakeAnnotationRepository:
    """Record repository calls and return controlled results in service tests."""

    def __init__(self, current_record: CurrentRecord | None) -> None:
        self.current_record = current_record
        self.active_page = Page(
            items=() if current_record is None else (current_record,),
            total=0 if current_record is None else 1,
        )
        self.version_page = Page(items=(_version_record(1),), total=1)
        self.versions = {1: _version_record(1)}
        self.annotation_is_known = current_record is not None
        self.last_annotation: Annotation | None = None
        self.last_actor_id: str | None = None
        self.last_owning_group_id: str | None = None
        self.last_expected_version: int | None = None
        self.last_filters: AnnotationSearchFilters | None = None
        self.last_limit: int | None = None
        self.last_offset: int | None = None
        self.get_count = 0
        self.last_include_deleted: bool | None = None

    def create_direct(
        self,
        *,
        annotation: Annotation,
        actor_id: str,
        owning_group_id: str,
    ) -> CurrentRecord:
        self.last_annotation = annotation
        self.last_actor_id = actor_id
        self.last_owning_group_id = owning_group_id
        self.current_record = _current_record(
            annotation_data=annotation.model_dump(mode="json")
        )
        self.current_record.owning_group_id = owning_group_id
        return self.current_record

    def get(
        self,
        annotation_id: UUID,
        *,
        include_deleted: bool = False,
    ) -> CurrentRecord | None:
        self.get_count += 1
        if (
            self.current_record is None
            or self.current_record.annotation_id != annotation_id
        ):
            return None
        if self.current_record.status == "deleted" and not include_deleted:
            return None
        return self.current_record

    def update_direct(
        self,
        annotation_id: UUID,
        annotation: Annotation,
        *,
        expected_version: int,
        actor_id: str,
    ) -> CurrentRecord:
        assert self.current_record is not None
        assert self.current_record.annotation_id == annotation_id
        self.last_annotation = annotation
        self.last_actor_id = actor_id
        self.last_expected_version = expected_version
        self.current_record.annotation_data = annotation.model_dump(mode="json")
        self.current_record.current_version += 1
        return self.current_record

    def soft_delete_direct(
        self,
        annotation_id: UUID,
        *,
        expected_version: int,
        actor_id: str,
    ) -> CurrentRecord:
        assert self.current_record is not None
        assert self.current_record.annotation_id == annotation_id
        self.last_actor_id = actor_id
        self.last_expected_version = expected_version
        self.current_record.current_version += 1
        self.current_record.status = "deleted"
        self.current_record.deleted_at = UPDATED_AT
        return self.current_record

    def list_active(
        self,
        filters: AnnotationSearchFilters,
        *,
        limit: int,
        offset: int,
    ) -> Page[CurrentRecord]:
        self.last_filters = filters
        self.last_limit = limit
        self.last_offset = offset
        return self.active_page

    def annotation_exists(
        self,
        annotation_id: UUID,
        *,
        include_deleted: bool = True,
    ) -> bool:
        self.last_include_deleted = include_deleted
        return (
            self.annotation_is_known
            and annotation_id == FIXED_ID
            and (include_deleted or self.current_record is not None)
        )

    def list_versions_page(
        self,
        annotation_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> Page[VersionRecord]:
        self.last_limit = limit
        self.last_offset = offset
        if annotation_id != FIXED_ID:
            return Page(items=(), total=0)
        return self.version_page

    def get_version(
        self,
        annotation_id: UUID,
        version: int,
    ) -> VersionRecord | None:
        if annotation_id != FIXED_ID:
            return None
        return self.versions.get(version)


@dataclass(slots=True)
class FakeUnitOfWork:
    annotations: FakeAnnotationRepository
    commit_count: int = 0

    @property
    def committed(self) -> bool:
        return self.commit_count > 0

    def __enter__(self) -> "FakeUnitOfWork":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def commit(self) -> None:
        self.commit_count += 1


@dataclass(slots=True)
class ServiceHarness:
    service: AnnotationService
    repository: FakeAnnotationRepository
    unit_of_work: FakeUnitOfWork


@pytest.fixture
def service_harness() -> ServiceHarness:
    repository = FakeAnnotationRepository(current_record=_current_record())
    unit_of_work = FakeUnitOfWork(annotations=repository)
    factory = cast(UnitOfWorkFactory, lambda: unit_of_work)
    service = AnnotationService(unit_of_work_factory=factory)
    return ServiceHarness(service, repository, unit_of_work)


def test_create_validates_payload_commits_and_returns_a_domain_result(
    service_harness: ServiceHarness,
) -> None:
    result = service_harness.service.create(
        payload=_valid_payload(),
        owning_group_id="curation-group",
        context=REQUEST_CONTEXT,
    )

    assert result.annotation_id == FIXED_ID
    assert result.version == 1
    assert result.owning_group_id == "curation-group"
    assert result.record_origin == "direct"
    assert result.source_import_job_id is None
    assert result.annotation.db_object_id == "UniProtKB:P12345"
    assert service_harness.repository.last_actor_id == "provisional-api-user"
    assert isinstance(service_harness.repository.last_annotation, Annotation)
    assert service_harness.unit_of_work.commit_count == 1


def test_create_rejects_invalid_payload_without_persisting_or_committing(
    service_harness: ServiceHarness,
) -> None:
    with pytest.raises(InvalidAnnotationPayloadError) as raised:
        service_harness.service.create(
            payload={"db_object_id": None},
            owning_group_id="curation-group",
            context=REQUEST_CONTEXT,
        )

    assert raised.value.errors[0]["location"] == ("db_object_id",)
    assert service_harness.repository.last_annotation is None
    assert service_harness.unit_of_work.commit_count == 0


def test_get_returns_a_domain_result_without_committing(
    service_harness: ServiceHarness,
) -> None:
    result = service_harness.service.get(FIXED_ID)

    assert result.annotation_id == FIXED_ID
    assert result.version == 1
    assert result.created_at == CREATED_AT
    assert result.updated_at == UPDATED_AT
    assert result.annotation.assigned_by == "GO_Central"
    assert service_harness.unit_of_work.commit_count == 0


def test_get_raises_for_an_unavailable_current_annotation(
    service_harness: ServiceHarness,
) -> None:
    service_harness.repository.current_record = None

    with pytest.raises(AnnotationNotFoundError):
        service_harness.service.get(FIXED_ID)

    assert service_harness.unit_of_work.commit_count == 0


def test_patch_replaces_a_supplied_list_and_keeps_omitted_fields(
    service_harness: ServiceHarness,
) -> None:
    result = service_harness.service.patch(
        FIXED_ID,
        changes={"references": ["PMID:9"]},
        expected_version=1,
        context=REQUEST_CONTEXT,
    )

    assert result.version == 2
    assert result.annotation.references == ["PMID:9"]
    assert result.annotation.assigned_by == "GO_Central"
    assert service_harness.repository.last_expected_version == 1
    assert service_harness.repository.last_actor_id == "provisional-api-user"
    assert service_harness.unit_of_work.commit_count == 1


def test_patch_replaces_a_supplied_top_level_object(
    service_harness: ServiceHarness,
) -> None:
    result = service_harness.service.patch(
        FIXED_ID,
        changes={"annotation_properties": {"contributor_id": ["GOC:tester"]}},
        expected_version=1,
        context=REQUEST_CONTEXT,
    )

    properties = result.annotation.annotation_properties
    assert properties is not None
    assert properties.contributor_id == ["GOC:tester"]
    assert properties.comment is None
    assert service_harness.unit_of_work.commit_count == 1


def test_patch_rejects_an_empty_change_object_before_reading(
    service_harness: ServiceHarness,
) -> None:
    with pytest.raises(EmptyAnnotationPatchError):
        service_harness.service.patch(
            FIXED_ID,
            changes={},
            expected_version=1,
            context=REQUEST_CONTEXT,
        )

    assert service_harness.repository.get_count == 0
    assert service_harness.unit_of_work.commit_count == 0


def test_patch_reports_validation_errors_from_the_complete_merged_result(
    service_harness: ServiceHarness,
) -> None:
    with pytest.raises(InvalidAnnotationPayloadError) as raised:
        service_harness.service.patch(
            FIXED_ID,
            changes={"db_object_id": None},
            expected_version=1,
            context=REQUEST_CONTEXT,
        )

    assert raised.value.errors[0]["location"] == ("db_object_id",)
    assert service_harness.repository.last_annotation is None
    assert service_harness.unit_of_work.commit_count == 0


def test_delete_commits_exactly_once_after_a_successful_soft_delete(
    service_harness: ServiceHarness,
) -> None:
    result = service_harness.service.delete(
        FIXED_ID,
        expected_version=1,
        context=REQUEST_CONTEXT,
    )

    assert result is None
    assert service_harness.repository.current_record is not None
    assert service_harness.repository.current_record.status == "deleted"
    assert service_harness.repository.current_record.current_version == 2
    assert service_harness.repository.last_expected_version == 1
    assert service_harness.repository.last_actor_id == "provisional-api-user"
    assert service_harness.unit_of_work.commit_count == 1


def test_list_maps_every_filter_and_preserves_page_metadata(
    service_harness: ServiceHarness,
) -> None:
    current_record = service_harness.repository.current_record
    assert current_record is not None
    service_harness.repository.active_page = Page(
        items=(current_record,),
        total=7,
    )

    result = service_harness.service.list(
        db_object_id="UniProtKB:P12345",
        negation=False,
        relation="RO:0002331",
        ontology_class_id="GO:0008150",
        evidence_type="ECO:0000314",
        annotation_date=date(2026, 9, 9),
        assigned_by="GO_Central",
        references=("PMID:1", "PMID:2"),
        with_or_from=("UniProtKB:Q1",),
        interacting_taxon_id=("NCBITaxon:9606",),
        limit=25,
        offset=50,
    )

    assert service_harness.repository.last_filters == AnnotationSearchFilters(
        db_object_id="UniProtKB:P12345",
        negation=False,
        relation="RO:0002331",
        ontology_class_id="GO:0008150",
        evidence_type="ECO:0000314",
        annotation_date=date(2026, 9, 9),
        assigned_by="GO_Central",
        references=("PMID:1", "PMID:2"),
        with_or_from=("UniProtKB:Q1",),
        interacting_taxon_id=("NCBITaxon:9606",),
    )
    assert [item.annotation_id for item in result.items] == [FIXED_ID]
    assert result.items[0].annotation.db_object_id == "UniProtKB:P12345"
    assert result.total == 7
    assert result.limit == 25
    assert result.offset == 50
    assert service_harness.unit_of_work.commit_count == 0


def test_list_versions_returns_deleted_history_without_committing(
    service_harness: ServiceHarness,
) -> None:
    assert service_harness.repository.current_record is not None
    service_harness.repository.current_record.status = "deleted"
    service_harness.repository.version_page = Page(
        items=(
            _version_record(2),
            _version_record(3, is_deleted=True),
        ),
        total=3,
    )

    result = service_harness.service.list_versions(FIXED_ID, limit=2, offset=1)

    assert [(item.version, item.is_deleted) for item in result.items] == [
        (2, False),
        (3, True),
    ]
    assert result.items[1].annotation.db_object_id == "UniProtKB:P12345"
    assert result.total == 3
    assert result.limit == 2
    assert result.offset == 1
    assert service_harness.repository.last_include_deleted is True
    assert service_harness.unit_of_work.commit_count == 0


def test_list_versions_raises_when_annotation_history_is_unknown(
    service_harness: ServiceHarness,
) -> None:
    service_harness.repository.annotation_is_known = False

    with pytest.raises(AnnotationHistoryNotFoundError) as raised:
        service_harness.service.list_versions(FIXED_ID, limit=50, offset=0)

    assert raised.value.annotation_id == FIXED_ID
    assert raised.value.version is None
    assert service_harness.unit_of_work.commit_count == 0


def test_get_version_returns_a_deleted_snapshot_without_committing(
    service_harness: ServiceHarness,
) -> None:
    service_harness.repository.versions[3] = _version_record(3, is_deleted=True)

    result = service_harness.service.get_version(FIXED_ID, 3)

    assert result.annotation_id == FIXED_ID
    assert result.version == 3
    assert result.is_deleted is True
    assert result.actor_id == "provisional-api-user"
    assert result.change_source == "api"
    assert result.annotation.assigned_by == "GO_Central"
    assert service_harness.repository.last_include_deleted is True
    assert service_harness.unit_of_work.commit_count == 0


def test_get_version_distinguishes_unknown_history_from_an_unknown_version(
    service_harness: ServiceHarness,
) -> None:
    with pytest.raises(AnnotationHistoryNotFoundError) as missing_version:
        service_harness.service.get_version(FIXED_ID, 9)

    assert missing_version.value.annotation_id == FIXED_ID
    assert missing_version.value.version == 9

    service_harness.repository.annotation_is_known = False
    with pytest.raises(AnnotationHistoryNotFoundError) as missing_history:
        service_harness.service.get_version(FIXED_ID, 1)

    assert missing_history.value.annotation_id == FIXED_ID
    assert missing_history.value.version is None
    assert service_harness.unit_of_work.commit_count == 0
