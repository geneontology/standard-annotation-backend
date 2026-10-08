"""Test annotation operations independently from HTTP and PostgreSQL."""

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from types import TracebackType
from typing import cast
from uuid import UUID

import pytest

from standard_annotation_backend.domain.annotations import (
    Annotation,
    InvalidAnnotationPayloadError,
)
from standard_annotation_backend.domain.audit import AuditAction, AuditResult
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
    RequestContext,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_service import (
    AnnotationService,
)

FIXED_ID = UUID("00000000-0000-0000-0000-000000000301")
CREATED_AT = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
UPDATED_AT = datetime(2026, 9, 11, 10, 5, tzinfo=UTC)
REQUEST_CONTEXT = RequestContext(
    actor_id="provisional-api-user",
    token_id=FIXED_ID,
    token_name="Test token",
    role=AuthorizationRole.ADMIN,
    scope=AuthorizationScope.GLOBAL,
    group_id=None,
)


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


class FakeAnnotationRepository:
    """Record repository calls and return controlled results in service tests."""

    def __init__(self, current_record: CurrentRecord | None) -> None:
        self.current_record = current_record
        self.last_annotation: Annotation | None = None

    def create(
        self,
        *,
        annotation: Annotation,
        actor_id: str,
        owning_group_id: str,
    ) -> CurrentRecord:
        self.last_annotation = annotation
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
        if (
            self.current_record is None
            or self.current_record.annotation_id != annotation_id
        ):
            return None
        return self.current_record

    def update(
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
        self.current_record.annotation_data = annotation.model_dump(mode="json")
        self.current_record.current_version += 1
        return self.current_record


@dataclass(frozen=True, slots=True)
class AuditCall:
    action: AuditAction
    actor_id: str
    result: AuditResult
    token_id: str | None
    token_name: str | None
    selected_role: str | None
    selected_scope: str | None
    selected_group_id: str | None
    annotation_id: UUID | None
    annotation_version: int | None
    comment_id: UUID | None
    job_id: UUID | None
    change_set_id: UUID | None
    details: dict[str, object] | None


class FakeAuditRepository:
    """Retain audit writes made by the service under test."""

    def __init__(self) -> None:
        self.calls: list[AuditCall] = []

    def record(
        self,
        *,
        action: AuditAction,
        actor_id: str,
        result: AuditResult,
        token_id: str | None = None,
        token_name: str | None = None,
        selected_role: str | None = None,
        selected_scope: str | None = None,
        selected_group_id: str | None = None,
        annotation_id: UUID | None = None,
        annotation_version: int | None = None,
        comment_id: UUID | None = None,
        job_id: UUID | None = None,
        change_set_id: UUID | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        self.calls.append(
            AuditCall(
                action=action,
                actor_id=actor_id,
                result=result,
                token_id=token_id,
                token_name=token_name,
                selected_role=selected_role,
                selected_scope=selected_scope,
                selected_group_id=selected_group_id,
                annotation_id=annotation_id,
                annotation_version=annotation_version,
                comment_id=comment_id,
                job_id=job_id,
                change_set_id=change_set_id,
                details=details,
            )
        )


@dataclass(slots=True)
class FakeUnitOfWork:
    annotations: FakeAnnotationRepository
    audit: FakeAuditRepository
    commit_count: int = 0

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
    unit_of_work = FakeUnitOfWork(
        annotations=repository,
        audit=FakeAuditRepository(),
    )
    factory = cast(UnitOfWorkFactory, lambda: unit_of_work)
    service = AnnotationService(unit_of_work_factory=factory)
    return ServiceHarness(service, repository, unit_of_work)


def test_create_rejects_invalid_payload_without_persisting_or_committing(
    service_harness: ServiceHarness,
) -> None:
    with pytest.raises(InvalidAnnotationPayloadError) as raised:
        service_harness.service.create(
            payload={"db_object_id": None},
            owning_group_id="curation-group",
            context=REQUEST_CONTEXT,
        )

    assert raised.value.errors[0]["location"] == ("annotation", "db_object_id")
    assert service_harness.repository.last_annotation is None
    assert service_harness.unit_of_work.commit_count == 0


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


@pytest.mark.parametrize("scope", [AuthorizationScope.SELF, AuthorizationScope.GROUP])
def test_create_derives_selected_group_and_records_token_context(
    service_harness: ServiceHarness,
    scope: AuthorizationScope,
) -> None:
    """Restricted creation derives ownership and records the complete bearer selection."""
    context = replace(REQUEST_CONTEXT, scope=scope, group_id="group-1")
    result = service_harness.service.create(payload=_valid_payload(), context=context)
    assert result.owning_group_id == "group-1"
    assert service_harness.unit_of_work.audit.calls[0].selected_scope == scope.value
    assert service_harness.unit_of_work.audit.calls[0].selected_group_id == "group-1"
