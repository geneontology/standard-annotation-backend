"""Verify atomic change-set review workflows with PostgreSQL."""

from importlib import import_module
from types import TracebackType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationVersionRecord,
    AuditEventRecord,
    ChangeSetRecord,
)
from standard_annotation_backend.persistence.repositories import (
    AnnotationRepository,
    DuplicateAnnotationError,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.annotation_service import (
    AnnotationService,
    RequestContext,
)
from standard_annotation_backend.services.change_set_service import (
    ChangeSet,
    ChangeSetService,
    InvalidChangeSetError,
)

PROPOSER = RequestContext(actor_id="proposer")
REVIEWER = RequestContext(actor_id="reviewer")


def _service(factory: UnitOfWorkFactory) -> ChangeSetService:
    return ChangeSetService(factory)


def test_create_preview_preserves_annotations_and_acceptance_records_history(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    """Only acceptance creates an annotation, with linked review and audit history."""
    service = _service(unit_of_work_factory)
    proposed = service.propose_create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="curator-group",
        reason="New evidence",
        context=PROPOSER,
    )
    assert proposed.state == "proposed"
    assert proposed.annotation_id is None
    assert proposed.proposed_by == "proposer"
    preview = service.preview(proposed.change_set_id)
    assert preview.can_accept
    assert preview.annotation == validated_annotation.model_dump(mode="json")
    assert preview.validation_errors == ()
    assert preview.duplicate_peer_ids == ()
    stored = service.get(proposed.change_set_id)
    assert stored.state == "proposed"
    assert stored.preview == preview
    assert stored.previewed_at is not None
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AnnotationRecord)) == 0
        assert (
            session.scalar(select(func.count()).select_from(AnnotationVersionRecord))
            == 0
        )

    accepted = service.accept(proposed.change_set_id, context=REVIEWER)
    assert accepted.change_set.state == "accepted"
    assert accepted.change_set.reviewed_by == "reviewer"
    assert accepted.change_set.annotation_id == accepted.annotation_id
    assert accepted.change_set.result_annotation_version == accepted.version == 1
    assert not accepted.is_deleted
    with session_factory() as session:
        annotation = session.get(AnnotationRecord, accepted.annotation_id)
        assert annotation is not None
        assert annotation.owning_group_id == "curator-group"
        version = session.scalar(select(AnnotationVersionRecord))
        assert version is not None
        assert version.change_source == "change_set"
        assert version.actor_id == "reviewer"
        events = list(
            session.scalars(
                select(AuditEventRecord).order_by(AuditEventRecord.created_at)
            )
        )
        assert [item.action for item in events] == [
            "change_set.proposed",
            "change_set.accepted",
        ]
        assert [item.actor_id for item in events] == ["proposer", "reviewer"]
        assert all(item.change_set_id == proposed.change_set_id for item in events)
        assert all(item.token_id is None for item in events)
        assert events[-1].annotation_id == accepted.annotation_id
        assert events[-1].annotation_version == 1


def test_invalid_create_preview_reports_validation_and_remains_reviewable(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Invalid annotation content is stored for review and reported in preview."""
    service = _service(unit_of_work_factory)
    proposed = service.propose_create(
        payload={"assigned_by": "TEST"},
        owning_group_id="group",
        reason="Candidate needs review",
        context=PROPOSER,
    )
    preview = service.preview(proposed.change_set_id)
    assert not preview.can_accept
    assert any(
        issue["location"] == ("db_object_id",) for issue in preview.validation_errors
    )
    module = import_module("standard_annotation_backend.services.change_set_service")
    with pytest.raises(module.InvalidChangeSetError):
        service.accept(proposed.change_set_id, context=REVIEWER)
    assert service.get(proposed.change_set_id).state == "proposed"


def test_create_acceptance_rechecks_duplicates_after_successful_preview(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """A duplicate created after preview prevents acceptance and preserves review."""
    service = _service(unit_of_work_factory)
    payload = validated_annotation.model_dump(mode="json")
    proposed = service.propose_create(
        payload=payload,
        owning_group_id="group",
        reason="New evidence",
        context=PROPOSER,
    )
    assert service.preview(proposed.change_set_id).can_accept
    duplicate = AnnotationService(unit_of_work_factory).create(
        payload=payload,
        owning_group_id="other-group",
        context=PROPOSER,
    )
    with pytest.raises(DuplicateAnnotationError) as error:
        service.accept(proposed.change_set_id, context=REVIEWER)
    assert error.value.peer_ids == (duplicate.annotation_id,)
    preview = service.preview(proposed.change_set_id)
    assert not preview.can_accept
    assert preview.duplicate_peer_ids == (duplicate.annotation_id,)
    assert service.get(proposed.change_set_id).state == "proposed"


@pytest.mark.parametrize(
    "failing_action", ["change_set.proposed", "change_set.accepted"]
)
def test_create_audit_failure_rolls_back_entire_workflow(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    failing_action: str,
) -> None:
    """Audit failure rolls back proposal or acceptance alongside annotation state."""
    service = _service(unit_of_work_factory)

    def propose() -> ChangeSet:
        return service.propose_create(
            payload=validated_annotation.model_dump(mode="json"),
            owning_group_id="group",
            reason="New evidence",
            context=PROPOSER,
        )

    proposed = propose() if failing_action == "change_set.accepted" else None

    def fail_audit(
        _mapper: object, _connection: object, record: AuditEventRecord
    ) -> None:
        if record.action == failing_action:
            raise RuntimeError("audit unavailable")

    event.listen(AuditEventRecord, "before_insert", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            if proposed is None:
                propose()
            else:
                service.accept(proposed.change_set_id, context=REVIEWER)
    finally:
        event.remove(AuditEventRecord, "before_insert", fail_audit)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AnnotationRecord)) == 0
        assert (
            session.scalar(select(func.count()).select_from(AnnotationVersionRecord))
            == 0
        )
        assert session.scalar(select(func.count()).select_from(ChangeSetRecord)) == (
            0 if proposed is None else 1
        )
    if proposed is not None:
        assert service.get(proposed.change_set_id).state == "proposed"


@pytest.mark.parametrize("change_source", ["api", "change_set"])
def test_direct_repository_create_records_selected_workflow(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    change_source: str,
) -> None:
    """The shared duplicate-safe write retains API defaults and explicit workflow."""
    with unit_of_work_factory() as unit_of_work:
        kwargs = {} if change_source == "api" else {"change_source": change_source}
        created = unit_of_work.annotations.create_direct(
            annotation=validated_annotation,
            actor_id="writer",
            owning_group_id="group",
            **kwargs,
        )
        version = unit_of_work.annotations.get_version(created.annotation_id, 1)
        assert version is not None
        assert version.change_source == change_source


def test_update_preview_and_acceptance_use_base_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """An update previews whole-field replacements and changes only on acceptance."""
    annotations = AnnotationService(unit_of_work_factory)
    target = annotations.create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="target-group",
        context=PROPOSER,
    )
    service = _service(unit_of_work_factory)
    proposed = service.propose_update(
        target.annotation_id,
        base_version=1,
        patch=[
            {"op": "test", "path": "/assigned_by", "value": "GO_Central"},
            {"op": "replace", "path": "/assigned_by", "value": "NEW"},
        ],
        reason="Correct attribution",
        context=PROPOSER,
    )
    assert proposed.owning_group_id == "target-group"
    preview = service.preview(proposed.change_set_id)
    assert preview.can_accept
    assert preview.before_annotation is not None
    assert preview.annotation is not None
    assert preview.before_annotation["assigned_by"] == "GO_Central"
    assert preview.annotation["assigned_by"] == "NEW"
    assert annotations.get(target.annotation_id).version == 1
    accepted = service.accept(proposed.change_set_id, context=REVIEWER)
    assert accepted.annotation_id == target.annotation_id
    assert accepted.version == 2
    assert accepted.annotation.assigned_by == "NEW"
    with unit_of_work_factory() as unit_of_work:
        version = unit_of_work.annotations.get_version(target.annotation_id, 2)
        assert version is not None
        assert version.change_source == "change_set"
        assert version.actor_id == "reviewer"


def test_false_boolean_number_test_does_not_persist_proposal_or_audit(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    """A failed JSON test rejects proposal persistence and its audit event together."""
    annotations = AnnotationService(unit_of_work_factory)
    target = annotations.create(
        payload={**validated_annotation.model_dump(mode="json"), "negation": False},
        owning_group_id="group",
        context=PROPOSER,
    )

    with pytest.raises(InvalidChangeSetError) as raised:
        _service(unit_of_work_factory).propose_update(
            target.annotation_id,
            base_version=1,
            patch=[
                {"op": "test", "path": "/negation", "value": 0},
                {"op": "replace", "path": "/assigned_by", "value": "NEW"},
            ],
            reason="Conditional correction",
            context=PROPOSER,
        )

    assert raised.value.errors[0]["location"] == ("patch",)
    assert raised.value.errors[0]["type"] == "invalid_patch_operation"
    assert annotations.get(target.annotation_id) == target
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ChangeSetRecord)) == 0
        assert list(session.scalars(select(AuditEventRecord.action))) == [
            "annotation.created"
        ]


def test_update_preview_reports_invalid_post_patch_annotation(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """A valid patch that removes a required field yields validation problems."""
    target = AnnotationService(unit_of_work_factory).create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="group",
        context=PROPOSER,
    )
    service = _service(unit_of_work_factory)
    proposed = service.propose_update(
        target.annotation_id,
        base_version=1,
        patch=[{"op": "remove", "path": "/db_object_id"}],
        reason="Review candidate",
        context=PROPOSER,
    )
    preview = service.preview(proposed.change_set_id)
    assert not preview.can_accept
    assert preview.validation_errors[0]["location"] == ("db_object_id",)
    module = import_module("standard_annotation_backend.services.change_set_service")
    with pytest.raises(module.InvalidChangeSetError):
        service.accept(proposed.change_set_id, context=REVIEWER)
    assert service.get(proposed.change_set_id).state == "proposed"


def test_update_preview_uses_historical_base_and_reports_current_version(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """A historical proposal previews its saved base while reporting staleness."""
    annotations = AnnotationService(unit_of_work_factory)
    target = annotations.create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="group",
        context=PROPOSER,
    )
    annotations.patch(
        target.annotation_id,
        changes={"assigned_by": "CURRENT"},
        expected_version=1,
        context=PROPOSER,
    )
    service = _service(unit_of_work_factory)
    proposed = service.propose_update(
        target.annotation_id,
        base_version=1,
        patch=[{"op": "replace", "path": "/annotation_date", "value": "2026-09-15"}],
        reason="Old snapshot",
        context=PROPOSER,
    )
    preview = service.preview(proposed.change_set_id)
    assert preview.is_stale
    assert not preview.can_accept
    assert preview.current_version == 2
    assert preview.before_annotation is not None
    assert preview.annotation is not None
    assert (
        preview.before_annotation["assigned_by"]
        == preview.annotation["assigned_by"]
        == "GO_Central"
    )
    assert service.get(proposed.change_set_id).state == "proposed"


@pytest.mark.parametrize("introduce_peer", [False, True])
def test_update_duplicate_policy_preserves_legacy_peers_and_rejects_new_peers(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    introduce_peer: bool,
) -> None:
    """Preview and acceptance allow existing duplicates but reject new conflicts."""
    payload = validated_annotation.model_dump(mode="json")
    with unit_of_work_factory() as unit_of_work:
        target = unit_of_work.annotations.create(
            annotation=validated_annotation,
            owning_group_id="group",
            actor_id="importer",
            change_source="import",
            record_origin="direct",
        )
        target_id = target.annotation_id
        legacy = unit_of_work.annotations.create(
            annotation=validated_annotation,
            owning_group_id="other",
            actor_id="importer",
            change_source="import",
            record_origin="direct",
        )
        new_peer = unit_of_work.annotations.create(
            annotation=Annotation.model_validate(
                {**payload, "references": ["PMID:99"]}
            ),
            owning_group_id="other",
            actor_id="importer",
            change_source="import",
            record_origin="direct",
        )
        legacy_id, new_peer_id = legacy.annotation_id, new_peer.annotation_id
        unit_of_work.commit()
    service = _service(unit_of_work_factory)
    patch = (
        [{"op": "replace", "path": "/references", "value": ["PMID:1", "PMID:99"]}]
        if introduce_peer
        else [{"op": "replace", "path": "/assigned_by", "value": "NEW"}]
    )
    proposed = service.propose_update(
        target_id,
        base_version=1,
        patch=patch,
        reason="Review duplicates",
        context=PROPOSER,
    )
    preview = service.preview(proposed.change_set_id)
    assert legacy_id not in preview.duplicate_peer_ids
    assert preview.duplicate_peer_ids == ((new_peer_id,) if introduce_peer else ())
    assert preview.can_accept is not introduce_peer
    if introduce_peer:
        with pytest.raises(DuplicateAnnotationError) as error:
            service.accept(proposed.change_set_id, context=REVIEWER)
        assert error.value.peer_ids == (new_peer_id,)
        assert service.get(proposed.change_set_id).state == "proposed"
    else:
        assert service.accept(proposed.change_set_id, context=REVIEWER).version == 2


@pytest.mark.parametrize(
    "patch",
    [
        [{"op": "remove", "path": "/references/0"}],
        [{"op": "invalid", "path": "/assigned_by"}],
        [{"op": "test", "path": "/assigned_by", "value": "wrong"}],
    ],
)
def test_update_proposal_rejects_unusable_patch_before_persistence(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    patch: object,
) -> None:
    """Unsupported or inapplicable patches are rejected without storing a proposal."""
    target = AnnotationService(unit_of_work_factory).create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="group",
        context=PROPOSER,
    )
    module = import_module("standard_annotation_backend.services.change_set_service")
    with pytest.raises(module.InvalidChangeSetError) as error:
        _service(unit_of_work_factory).propose_update(
            target.annotation_id,
            base_version=1,
            patch=patch,
            reason="Bad patch",
            context=PROPOSER,
        )
    assert error.value.errors
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ChangeSetRecord)) == 0


def test_update_proposal_requires_existing_base_version(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """A proposal cannot target a version that has never existed."""
    target = AnnotationService(unit_of_work_factory).create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="group",
        context=PROPOSER,
    )
    module = import_module("standard_annotation_backend.services.change_set_service")
    with pytest.raises(module.InvalidChangeSetError) as error:
        _service(unit_of_work_factory).propose_update(
            target.annotation_id,
            base_version=99,
            patch=[{"op": "replace", "path": "/assigned_by", "value": "NEW"}],
            reason="Unknown base",
            context=PROPOSER,
        )
    assert error.value.errors[0]["type"] == "base_version_not_found"


@pytest.mark.parametrize("change_source", ["api", "change_set"])
def test_direct_repository_update_records_selected_workflow(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    change_source: str,
) -> None:
    """Shared update policy retains the default API source or the selected source."""
    with unit_of_work_factory() as unit_of_work:
        target = unit_of_work.annotations.create_direct(
            annotation=validated_annotation, actor_id="writer", owning_group_id="group"
        )
        kwargs = {} if change_source == "api" else {"change_source": change_source}
        unit_of_work.annotations.update_direct(
            target.annotation_id,
            validated_annotation,
            actor_id="writer",
            expected_version=1,
            **kwargs,
        )
        version = unit_of_work.annotations.get_version(target.annotation_id, 2)
        assert version is not None
        assert version.change_source == change_source


def test_delete_preview_and_acceptance_preserve_history(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """Deletion previews its target, then saves a deleted version upon acceptance."""
    annotations = AnnotationService(unit_of_work_factory)
    target = annotations.create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="target-group",
        context=PROPOSER,
    )
    service = _service(unit_of_work_factory)
    proposal = service.propose_delete(
        target.annotation_id,
        base_version=1,
        reason="Withdraw evidence",
        context=PROPOSER,
    )
    assert proposal.owning_group_id == "target-group"
    preview = service.preview(proposal.change_set_id)
    assert preview.can_accept
    assert preview.is_deleted
    assert preview.annotation == validated_annotation.model_dump(mode="json")
    assert annotations.get(target.annotation_id).version == 1
    accepted = service.accept(proposal.change_set_id, context=REVIEWER)
    assert accepted.version == 2
    assert accepted.is_deleted
    assert accepted.change_set.result_annotation_version == 2
    with unit_of_work_factory() as unit_of_work:
        assert unit_of_work.annotations.get(target.annotation_id) is None
        version = unit_of_work.annotations.get_version(target.annotation_id, 2)
        assert version is not None
        assert version.is_deleted
        assert version.change_source == "change_set"


def test_rejection_records_explanation_reviewer_and_audit(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    """Rejection stores the reviewer's explanation and leaves annotations untouched."""
    service = _service(unit_of_work_factory)
    proposal = service.propose_create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="group",
        reason="Candidate",
        context=PROPOSER,
    )
    rejected = service.reject(
        proposal.change_set_id,
        review_reason="Evidence does not support this annotation",
        context=REVIEWER,
    )
    assert rejected.state == "rejected"
    assert rejected.reviewed_by == "reviewer"
    assert rejected.review_reason == "Evidence does not support this annotation"
    assert rejected.reviewed_at is not None
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AnnotationRecord)) == 0
        audit = session.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.action == "change_set.rejected"
            )
        )
        assert audit is not None
        assert audit.actor_id == "reviewer"
        assert audit.change_set_id == proposal.change_set_id


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("target_change", ["update", "delete"])
def test_stale_acceptance_commits_review_and_audit_before_raising(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    operation: str,
    target_change: str,
) -> None:
    """A changed or deleted target makes review stale durably before the error escapes."""
    annotations = AnnotationService(unit_of_work_factory)
    target = annotations.create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="group",
        context=PROPOSER,
    )
    service = _service(unit_of_work_factory)
    proposal = getattr(service, f"propose_{operation}")(
        target.annotation_id,
        base_version=1,
        reason="Review",
        context=PROPOSER,
        **(
            {"patch": [{"op": "replace", "path": "/assigned_by", "value": "PROPOSED"}]}
            if operation == "update"
            else {}
        ),
    )
    if target_change == "update":
        annotations.patch(
            target.annotation_id,
            changes={"assigned_by": "CURRENT"},
            expected_version=1,
            context=PROPOSER,
        )
    else:
        annotations.delete(target.annotation_id, expected_version=1, context=PROPOSER)
    exits: list[type[BaseException] | None] = []

    class ObservedUnitOfWork(SqlAlchemyUnitOfWork):
        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            exits.append(exc_type)
            super().__exit__(exc_type, exc_value, traceback)

    module = import_module("standard_annotation_backend.services.change_set_service")
    observed_service = _service(lambda: ObservedUnitOfWork(session_factory))
    with pytest.raises(module.StaleChangeSetError) as error:
        observed_service.accept(proposal.change_set_id, context=REVIEWER)
    assert exits == [None]
    assert error.value.change_set_id == proposal.change_set_id
    assert error.value.expected_version == 1
    assert error.value.current_version == 2
    stored = service.get(proposal.change_set_id)
    assert stored.state == "stale"
    assert stored.reviewed_by == "reviewer"
    assert stored.preview is not None
    assert stored.preview.is_stale
    with session_factory() as session:
        target_record = session.get(AnnotationRecord, target.annotation_id)
        assert target_record is not None
        assert target_record.current_version == 2
        audit = session.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.action == "change_set.marked_stale"
            )
        )
        assert audit is not None
        assert audit.change_set_id == proposal.change_set_id
        assert audit.annotation_id == target.annotation_id


@pytest.mark.parametrize("state", ["accepted", "rejected", "stale"])
@pytest.mark.parametrize("operation", ["accept", "reject", "preview"])
def test_terminal_proposals_reject_further_review_operations(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    state: str,
    operation: str,
) -> None:
    """A completed review remains terminal and cannot be previewed or reviewed again."""
    annotations = AnnotationService(unit_of_work_factory)
    target = annotations.create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="group",
        context=PROPOSER,
    )
    service = _service(unit_of_work_factory)
    proposal = service.propose_delete(
        target.annotation_id, base_version=1, reason="Withdraw", context=PROPOSER
    )
    module = import_module("standard_annotation_backend.services.change_set_service")
    if state == "accepted":
        service.accept(proposal.change_set_id, context=REVIEWER)
    elif state == "rejected":
        service.reject(proposal.change_set_id, context=REVIEWER, review_reason="Retain")
    else:
        annotations.patch(
            target.annotation_id,
            changes={"assigned_by": "CURRENT"},
            expected_version=1,
            context=PROPOSER,
        )
        with pytest.raises(module.StaleChangeSetError):
            service.accept(proposal.change_set_id, context=REVIEWER)
    with pytest.raises(module.ChangeSetStateError) as error:
        getattr(service, operation)(
            proposal.change_set_id,
            **({} if operation == "preview" else {"context": REVIEWER}),
            **({"review_reason": "Again"} if operation == "reject" else {}),
        )
    assert error.value.state == state
    assert service.get(proposal.change_set_id).state == state


@pytest.mark.parametrize("operation", ["get", "preview", "accept", "reject"])
def test_unknown_change_set_has_transport_neutral_not_found_error(
    unit_of_work_factory: UnitOfWorkFactory,
    operation: str,
) -> None:
    """Read, preview, and review operations expose the missing change-set identifier."""
    service = _service(unit_of_work_factory)
    module = import_module("standard_annotation_backend.services.change_set_service")
    missing_id = uuid4()
    with pytest.raises(module.ChangeSetNotFoundError) as error:
        getattr(service, operation)(
            missing_id,
            **({"context": REVIEWER} if operation in {"accept", "reject"} else {}),
            **({"review_reason": "Missing"} if operation == "reject" else {}),
        )
    assert error.value.change_set_id == missing_id


@pytest.mark.parametrize("change_source", ["api", "change_set"])
def test_direct_repository_delete_records_selected_workflow(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    change_source: str,
) -> None:
    """Shared delete policy preserves the API default and explicit review source."""
    with unit_of_work_factory() as unit_of_work:
        target = unit_of_work.annotations.create_direct(
            annotation=validated_annotation, actor_id="writer", owning_group_id="group"
        )
        kwargs = {} if change_source == "api" else {"change_source": change_source}
        unit_of_work.annotations.soft_delete_direct(
            target.annotation_id, actor_id="writer", expected_version=1, **kwargs
        )
        version = unit_of_work.annotations.get_version(target.annotation_id, 2)
        assert version is not None
        assert version.change_source == change_source


def test_delete_proposal_requires_existing_active_base_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """Deletion proposals require a saved version that still represented an annotation."""
    annotations = AnnotationService(unit_of_work_factory)
    target = annotations.create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="group",
        context=PROPOSER,
    )
    annotations.delete(target.annotation_id, expected_version=1, context=PROPOSER)
    service = _service(unit_of_work_factory)
    module = import_module("standard_annotation_backend.services.change_set_service")
    for version, issue_type in [
        (99, "base_version_not_found"),
        (2, "base_version_deleted"),
    ]:
        with pytest.raises(module.InvalidChangeSetError) as error:
            service.propose_delete(
                target.annotation_id,
                base_version=version,
                reason="Invalid target",
                context=PROPOSER,
            )
        assert error.value.errors[0]["type"] == issue_type


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_repository_detected_stale_write_is_persisted_after_intervening_transaction(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """A write committed on another connection after the service read marks review stale."""
    target = AnnotationService(unit_of_work_factory).create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="group",
        context=PROPOSER,
    )
    service = _service(unit_of_work_factory)
    proposal = getattr(service, f"propose_{operation}")(
        target.annotation_id,
        base_version=1,
        reason="Review",
        context=PROPOSER,
        **(
            {"patch": [{"op": "replace", "path": "/assigned_by", "value": "PROPOSED"}]}
            if operation == "update"
            else {}
        ),
    )
    original_update = AnnotationRepository.update_direct
    original_delete = AnnotationRepository.soft_delete_direct
    candidate = Annotation.model_validate(
        {**validated_annotation.model_dump(mode="json"), "assigned_by": "CONCURRENT"}
    )

    def update_elsewhere() -> None:
        with unit_of_work_factory() as unit_of_work:
            original_update(
                unit_of_work.annotations,
                target.annotation_id,
                candidate,
                expected_version=1,
                actor_id="concurrent-writer",
            )
            unit_of_work.commit()

    def interrupted_update(
        repository: AnnotationRepository,
        annotation_id: UUID,
        annotation: Annotation,
        *,
        expected_version: int,
        actor_id: str,
        change_source: str = "api",
    ) -> AnnotationRecord:
        update_elsewhere()
        return original_update(
            repository,
            annotation_id,
            annotation,
            expected_version=expected_version,
            actor_id=actor_id,
            change_source=change_source,
        )

    def interrupted_delete(
        repository: AnnotationRepository,
        annotation_id: UUID,
        *,
        expected_version: int,
        actor_id: str,
        change_source: str = "api",
    ) -> AnnotationRecord:
        update_elsewhere()
        return original_delete(
            repository,
            annotation_id,
            expected_version=expected_version,
            actor_id=actor_id,
            change_source=change_source,
        )

    if operation == "update":
        monkeypatch.setattr(AnnotationRepository, "update_direct", interrupted_update)
    else:
        monkeypatch.setattr(
            AnnotationRepository, "soft_delete_direct", interrupted_delete
        )
    module = import_module("standard_annotation_backend.services.change_set_service")
    with pytest.raises(module.StaleChangeSetError) as error:
        service.accept(proposal.change_set_id, context=REVIEWER)
    assert error.value.current_version == 2
    stored = service.get(proposal.change_set_id)
    assert stored.state == "stale"
    assert stored.preview is not None
    assert stored.preview.current_version == 2
    assert stored.preview.is_stale


@pytest.mark.parametrize(
    "transition", ["accept_update", "accept_delete", "reject", "stale"]
)
def test_review_audit_failure_rolls_back_annotation_and_review_state(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    transition: str,
) -> None:
    """A failed audit record leaves the proposal and prior annotation version intact."""
    annotations = AnnotationService(unit_of_work_factory)
    target = annotations.create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="group",
        context=PROPOSER,
    )
    service = _service(unit_of_work_factory)
    proposal = (
        service.propose_update(
            target.annotation_id,
            base_version=1,
            patch=[{"op": "replace", "path": "/assigned_by", "value": "PROPOSED"}],
            reason="Review",
            context=PROPOSER,
        )
        if transition == "accept_update"
        else service.propose_delete(
            target.annotation_id, base_version=1, reason="Withdraw", context=PROPOSER
        )
    )
    if transition == "stale":
        annotations.patch(
            target.annotation_id,
            changes={"assigned_by": "CURRENT"},
            expected_version=1,
            context=PROPOSER,
        )

    def fail_audit(
        _mapper: object, _connection: object, _record: AuditEventRecord
    ) -> None:
        raise RuntimeError("audit unavailable")

    event.listen(AuditEventRecord, "before_insert", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            if transition == "reject":
                service.reject(
                    proposal.change_set_id,
                    review_reason="Keep target",
                    context=REVIEWER,
                )
            else:
                service.accept(proposal.change_set_id, context=REVIEWER)
    finally:
        event.remove(AuditEventRecord, "before_insert", fail_audit)
    stored = service.get(proposal.change_set_id)
    assert stored.state == "proposed"
    assert stored.preview is None
    assert stored.reviewed_at is None
    with session_factory() as session:
        current = session.get(AnnotationRecord, target.annotation_id)
        assert current is not None
        assert current.current_version == (2 if transition == "stale" else 1)
        assert current.status == "active"
