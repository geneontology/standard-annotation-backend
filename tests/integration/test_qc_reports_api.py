"""Verify starting QC reports and reading them within each reader's scope."""

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from refresh_helpers import TEST_SOURCES
from seeding import create_job, insert_annotation
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.api.dependencies import get_authenticated_context
from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
    RequestContext,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.main import app
from standard_annotation_backend.persistence.locks import bind_try_lock
from standard_annotation_backend.persistence.repositories import QcReportRepository
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.qc_report_service import QcReportRunner
from standard_annotation_backend.workers import tasks

PATH = "/reports/annotation-qc"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    """Record dispatched report jobs instead of sending them to Celery."""
    jobs: list[UUID] = []

    def record(job: Job) -> None:
        jobs.append(job.job_id)

    monkeypatch.setattr(tasks, "dispatch_qc_report", record)
    return jobs


def _use(role: str, scope: str, *, actor: str = "someone", group: str = "MGI") -> None:
    """Send the client's requests with a token of this role and scope.

    `integration_api_client` restores its own identity when the test ends.
    """
    context = RequestContext(
        actor_id=actor,
        token_id=uuid4(),
        token_name="Test",
        role=AuthorizationRole(role),
        scope=AuthorizationScope(scope),
        group_id=None if scope == "global" else group,
    )
    app.dependency_overrides[get_authenticated_context] = lambda: context


def _annotation(**changes: object) -> Annotation:
    return Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:NOPE",
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0006915",
            "references": ["PMID:1"],
            "evidence_type": "ECO:0000314",
            "annotation_date": "2026-11-12",
            "assigned_by": "MGI",
            **changes,
        }
    )


def _run(
    unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine, report_id: UUID
) -> None:
    QcReportRunner(
        unit_of_work_factory, TEST_SOURCES, bind_try_lock(database_engine)
    ).run(report_id)


def _start(client: TestClient, body: dict[str, object]) -> UUID:
    """Start a report as the client's identity and return its ID."""
    response = client.post(PATH, json=body)
    assert response.status_code == status.HTTP_202_ACCEPTED
    return UUID(response.json()["job_id"])


@pytest.fixture
def completed_report(
    integration_api_client: TestClient,
    sent: list[UUID],
    active_annotation_subjects: None,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> UUID:
    """Run an all-groups report over three annotations with issues in two groups.

    Two MGI annotations, one created by `someone` and one by `curator`, and one
    RGD annotation all name an unknown entity.
    """
    with session_factory() as session:
        insert_annotation(
            session, _annotation(), owning_group_id="MGI", actor_id="someone"
        )
        insert_annotation(
            session,
            _annotation(references=["PMID:2"]),
            owning_group_id="MGI",
            actor_id="curator",
        )
        insert_annotation(
            session,
            _annotation(references=["PMID:3"]),
            owning_group_id="RGD",
            actor_id="curator",
        )
        session.commit()
    response = integration_api_client.post(PATH, json={})
    assert response.status_code == status.HTTP_202_ACCEPTED
    report_id = UUID(response.json()["job_id"])
    _run(unit_of_work_factory, database_engine, report_id)
    return report_id


def test_a_global_admin_starts_reports_that_join_the_queued_one(
    integration_api_client: TestClient, sent: list[UUID]
) -> None:
    """Starting twice for one scope returns the same queued job; another scope is new."""
    first = integration_api_client.post(PATH, json={})
    again = integration_api_client.post(PATH, json={})
    group = integration_api_client.post(PATH, json={"owning_group_id": "MGI"})

    assert first.status_code == status.HTTP_202_ACCEPTED
    assert first.json()["job_type"] == "annotation_qc_report"
    assert first.json()["status"] == "queued"
    assert again.json()["job_id"] == first.json()["job_id"]
    assert group.json()["job_id"] != first.json()["job_id"]
    scope = integration_api_client.get(f"{PATH}/{group.json()['job_id']}").json()
    assert scope["owning_group_id"] == "MGI"
    assert sent == [
        UUID(first.json()["job_id"]),
        UUID(first.json()["job_id"]),
        UUID(group.json()["job_id"]),
    ]


@pytest.mark.parametrize(
    ("role", "scope", "body", "expected"),
    [
        ("admin", "group", {}, status.HTTP_202_ACCEPTED),
        ("admin", "self", {"owning_group_id": "MGI"}, status.HTTP_202_ACCEPTED),
        ("admin", "group", {"owning_group_id": "RGD"}, status.HTTP_403_FORBIDDEN),
        ("edit", "global", {}, status.HTTP_403_FORBIDDEN),
        ("read", "global", {}, status.HTTP_403_FORBIDDEN),
    ],
)
def test_only_admins_start_reports_and_group_admins_only_for_their_group(
    integration_api_client: TestClient,
    sent: list[UUID],
    role: str,
    scope: str,
    body: dict[str, object],
    expected: int,
) -> None:
    """A group-restricted admin's report always covers its own group."""
    _use(role, scope)
    response = integration_api_client.post(PATH, json=body)

    assert response.status_code == expected
    if expected == status.HTTP_202_ACCEPTED:
        report = integration_api_client.get(f"{PATH}/{response.json()['job_id']}")
        assert report.json()["owning_group_id"] == "MGI"
        assert len(sent) == 1
    else:
        assert response.json()["error"]["code"] == "permission_denied"
        assert sent == []


def test_the_summary_reports_versions_and_counts_by_type_and_group(
    integration_api_client: TestClient, completed_report: UUID
) -> None:
    """A global reader sees every group's counts and the reference data used."""
    response = integration_api_client.get(f"{PATH}/{completed_report}")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["report_id"] == str(completed_report)
    assert body["status"] == "succeeded"
    assert body["owning_group_id"] is None
    assert body["completed_at"] is not None
    assert (body["annotations_checked"], body["annotations_with_issues"]) == (3, 3)
    assert body["by_issue_type"] == {"unknown_entity": 3}
    assert body["by_group"] == {
        "MGI": {"annotations_checked": 2, "annotations_with_issues": 2},
        "RGD": {"annotations_checked": 1, "annotations_with_issues": 1},
    }
    assert body["sabe_version"]
    assert body["schema_version"]
    (versions,) = body["reference_data"]
    assert {use["ontology_key"] for use in versions["ontologies"]} >= {"go"}
    assert versions["go_site_revision"]


@pytest.mark.parametrize(
    ("scope", "visible"),
    [("self", 1), ("group", 2), ("global", 3)],
)
def test_readers_see_findings_within_their_scope_and_counts_by_group(
    integration_api_client: TestClient,
    completed_report: UUID,
    scope: str,
    visible: int,
) -> None:
    """Self readers list their own annotations' findings; counts are by group."""
    _use("read", scope)
    summary = integration_api_client.get(f"{PATH}/{completed_report}").json()
    findings = integration_api_client.get(f"{PATH}/{completed_report}/findings").json()

    assert findings["total"] == visible
    if scope == "global":
        assert set(summary["by_group"]) == {"MGI", "RGD"}
    else:
        assert set(summary["by_group"]) == {"MGI"}
        assert summary["annotations_with_issues"] == 2


def test_a_reader_of_another_group_sees_no_counts_or_findings(
    integration_api_client: TestClient, completed_report: UUID
) -> None:
    """A group with no annotations in the report sees an empty summary and page."""
    _use("read", "group", group="ZFIN")
    summary = integration_api_client.get(f"{PATH}/{completed_report}").json()
    findings = integration_api_client.get(
        f"{PATH}/{completed_report}/findings", params={"owning_group_id": "MGI"}
    ).json()

    assert summary["status"] == "succeeded"
    assert (summary["annotations_checked"], summary["by_group"]) == (0, {})
    assert summary["by_issue_type"] == {}
    assert findings["total"] == 0


def test_findings_filter_by_issue_type_and_group_and_page(
    integration_api_client: TestClient, completed_report: UUID
) -> None:
    """Filters combine with paging; an issue type with no findings lists nothing."""
    page = integration_api_client.get(
        f"{PATH}/{completed_report}/findings",
        params={"owning_group_id": "MGI", "issue_type": "unknown_entity", "limit": 1},
    ).json()
    none = integration_api_client.get(
        f"{PATH}/{completed_report}/findings", params={"issue_type": "gorule_0000002"}
    ).json()

    assert (page["total"], len(page["items"]), page["limit"]) == (2, 1, 1)
    (finding,) = page["items"]
    assert finding["owning_group_id"] == "MGI"
    assert finding["annotation_version"] == 1
    assert finding["issues"][0]["type"] == "unknown_entity"
    assert finding["issues"][0]["location"] == ["annotation", "db_object_id"]
    assert "created_by" not in finding
    assert none["total"] == 0


def test_findings_reject_unknown_query_parameters_and_issue_types(
    integration_api_client: TestClient, completed_report: UUID
) -> None:
    """Misspelled parameters and unknown issue types are invalid input."""
    unknown = integration_api_client.get(
        f"{PATH}/{completed_report}/findings", params={"issue": "unknown_entity"}
    )
    bad_type = integration_api_client.get(
        f"{PATH}/{completed_report}/findings", params={"issue_type": "nonsense"}
    )

    assert unknown.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert unknown.json()["error"]["code"] == "unknown_query_parameter"
    assert bad_type.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_older_findings_expire_but_the_summary_remains(
    integration_api_client: TestClient,
    completed_report: UUID,
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """After a newer all-groups report, the older one's findings return 410."""
    newer = UUID(integration_api_client.post(PATH, json={}).json()["job_id"])
    _run(unit_of_work_factory, database_engine, newer)

    expired = integration_api_client.get(f"{PATH}/{completed_report}/findings")
    summary = integration_api_client.get(f"{PATH}/{completed_report}")
    current = integration_api_client.get(f"{PATH}/{newer}/findings")

    assert expired.status_code == status.HTTP_410_GONE
    assert expired.json()["error"]["code"] == "report_findings_expired"
    assert summary.status_code == status.HTTP_200_OK
    assert summary.json()["annotations_with_issues"] == 3
    assert current.json()["total"] == 3


def test_unfinished_unknown_and_non_report_jobs(
    integration_api_client: TestClient,
    sent: list[UUID],
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Unfinished reports have no findings yet; other IDs are not reports."""
    queued = integration_api_client.post(PATH, json={}).json()["job_id"]
    other_job = create_job(
        unit_of_work_factory,
        job_type=JobType.RULES_REFRESH,
        parameters={"source_key": "go-site"},
    ).job_id

    not_completed = integration_api_client.get(f"{PATH}/{queued}/findings")
    queued_summary = integration_api_client.get(f"{PATH}/{queued}").json()
    unknown = integration_api_client.get(f"{PATH}/{uuid4()}")
    not_a_report = integration_api_client.get(f"{PATH}/{other_job}/findings")

    assert not_completed.status_code == status.HTTP_409_CONFLICT
    assert not_completed.json()["error"]["code"] == "report_not_completed"
    assert queued_summary["status"] == "queued"
    assert queued_summary["sabe_version"] is None
    assert queued_summary["reference_data"] == []
    assert unknown.status_code == status.HTTP_404_NOT_FOUND
    assert unknown.json()["error"]["code"] == "report_not_found"
    assert not_a_report.status_code == status.HTTP_404_NOT_FOUND
    assert not_a_report.json()["error"]["code"] == "report_not_found"


def test_a_report_finishing_during_a_read_never_yields_an_empty_page(
    integration_api_client: TestClient,
    completed_report: UUID,
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer report that completes mid-read gives the older findings or `410`."""
    newer = UUID(integration_api_client.post(PATH, json={}).json()["job_id"])
    original = QcReportRepository.has_newer_report
    finished: list[bool] = []

    def check_then_finish_newer(
        self: QcReportRepository,
        job_id: UUID,
        parameters: dict[str, object],
        completed_at: datetime,
    ) -> bool:
        found = original(self, job_id, parameters, completed_at)
        if not finished:
            finished.append(True)
            _run(unit_of_work_factory, database_engine, newer)
        return found

    monkeypatch.setattr(QcReportRepository, "has_newer_report", check_then_finish_newer)
    response = integration_api_client.get(f"{PATH}/{completed_report}/findings")

    assert finished
    if response.status_code == status.HTTP_200_OK:
        assert response.json()["total"] == 3
    else:
        assert response.status_code == status.HTTP_410_GONE


def test_reports_are_listed_newest_first_with_findings_availability(
    integration_api_client: TestClient,
    sent: list[UUID],
    active_annotation_subjects: None,
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """Only the latest succeeded report of a scope offers findings, and reads agree."""
    older = _start(integration_api_client, {})
    _run(unit_of_work_factory, database_engine, older)
    newer = _start(integration_api_client, {})
    _run(unit_of_work_factory, database_engine, newer)
    queued = _start(integration_api_client, {"owning_group_id": "MGI"})

    body = integration_api_client.get(PATH).json()

    assert body["total"] == 3
    assert [item["report_id"] for item in body["items"]] == [
        str(queued),
        str(newer),
        str(older),
    ]
    assert [item["findings_available"] for item in body["items"]] == [
        False,
        True,
        False,
    ]
    assert [item["owning_group_id"] for item in body["items"]] == ["MGI", None, None]
    assert body["items"][0]["status"] == "queued"
    newer_findings = integration_api_client.get(f"{PATH}/{newer}/findings")
    older_findings = integration_api_client.get(f"{PATH}/{older}/findings")
    assert newer_findings.status_code == status.HTTP_200_OK
    assert older_findings.status_code == status.HTTP_410_GONE


@pytest.mark.parametrize("scope", ["self", "group"])
def test_readers_list_all_groups_reports_and_their_own_groups(
    integration_api_client: TestClient, sent: list[UUID], scope: str
) -> None:
    """Restricted readers see reports that cover their group; global readers see all."""
    all_groups = _start(integration_api_client, {})
    mgi = _start(integration_api_client, {"owning_group_id": "MGI"})
    rgd = _start(integration_api_client, {"owning_group_id": "RGD"})

    everything = integration_api_client.get(PATH).json()["items"]
    _use("read", scope)
    visible = integration_api_client.get(PATH).json()

    assert {item["report_id"] for item in everything} == {
        str(all_groups),
        str(mgi),
        str(rgd),
    }
    assert {item["report_id"] for item in visible["items"]} == {
        str(all_groups),
        str(mgi),
    }
    assert visible["total"] == 2


def test_the_report_list_filters_by_status_and_pages(
    integration_api_client: TestClient, completed_report: UUID
) -> None:
    """A status filter and a page limit combine; unknown parameters are rejected."""
    _start(integration_api_client, {"owning_group_id": "MGI"})

    succeeded = integration_api_client.get(PATH, params={"status": "succeeded"}).json()
    first = integration_api_client.get(PATH, params={"limit": 1}).json()
    second = integration_api_client.get(PATH, params={"limit": 1, "offset": 1}).json()
    unknown = integration_api_client.get(PATH, params={"scope": "MGI"})

    assert [item["report_id"] for item in succeeded["items"]] == [str(completed_report)]
    assert (first["total"], len(first["items"]), first["limit"]) == (2, 1, 1)
    assert second["items"][0]["report_id"] == str(completed_report)
    assert first["items"][0]["report_id"] != second["items"][0]["report_id"]
    assert unknown.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert unknown.json()["error"]["code"] == "unknown_query_parameter"


@pytest.mark.parametrize("group", ["MGI ", " MGI"])
def test_a_report_scope_with_surrounding_whitespace_is_rejected(
    integration_api_client: TestClient, sent: list[UUID], group: str
) -> None:
    """A padded group key is invalid input, so no unreadable report scope is stored."""
    response = integration_api_client.post(PATH, json={"owning_group_id": group})
    listing = integration_api_client.get(PATH)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert sent == []
    assert listing.status_code == status.HTTP_200_OK
    assert listing.json()["total"] == 0
