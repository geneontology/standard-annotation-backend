"""Verify the refresh CLI runs jobs in-process with jobs and audit events."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from annotation_refresh_helpers import gpad_bytes, gpad_row, seed_group_import
from refresh_helpers import (
    TEST_SOURCES,
    FakeFetchers,
    build_runner,
    sources_with_entities,
)
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from test_entity_refresh_service import publish_catalog

from standard_annotation_backend.cli import refresh as cli
from standard_annotation_backend.domain.annotation_management import (
    AnnotationManagementMode,
)
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    JobRecord,
    SabUserRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import JobService

USERS_YAML = b"""
- accounts: {github: curator}
  authorizations:
    sab: [{role: admin, scope: global}]
"""
GPI = (Path(__file__).parents[1] / "fixtures" / "gpi" / "minimal.gpi").read_bytes()

type InstallRuntime = Callable[..., FakeFetchers]


@pytest.fixture
def use_runtime(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
) -> InstallRuntime:
    """Return a function that points the CLI at fake sources and the test database."""

    def install(
        contents: dict[str, bytes | Exception], sources: RefreshSources = TEST_SOURCES
    ) -> FakeFetchers:
        fetchers = FakeFetchers(contents)

        @contextmanager
        def runtime() -> Iterator[SimpleNamespace]:
            yield SimpleNamespace(
                unit_of_work_factory=unit_of_work_factory,
                settings=SimpleNamespace(sources=sources),
                jobs=JobService(unit_of_work_factory),
                runner=build_runner(database_engine, unit_of_work_factory, fetchers),
            )

        monkeypatch.setattr(cli, "worker_runtime", runtime)
        return fetchers

    return install


def test_cli_bootstraps_authorization_with_job_and_audit(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refreshing authorization from the CLI records jobs and audits like the API."""
    use_runtime({"go-site": USERS_YAML})

    assert cli.main(["authorization"]) == 0

    output = capsys.readouterr().out
    assert "authorization_refresh go-site succeeded" in output
    with session_factory() as session:
        assert session.scalar(select(SabUserRecord.github_login)) == "curator"
        job = session.scalar(select(JobRecord))
        assert job is not None and job.requested_by == "cli"
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEventRecord)
                .where(
                    AuditEventRecord.action == AuditAction.AUTHORIZATION_REFRESHED,
                    AuditEventRecord.actor_id == "cli",
                )
            )
            == 1
        )


def test_cli_exits_nonzero_and_prints_the_failure_code(
    use_runtime: InstallRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed job is printed with its code and makes the CLI exit with 1."""
    use_runtime({"mgi": b"not a gpi file\n", "rgd": GPI})

    assert cli.main(["entity"]) == 1

    lines = capsys.readouterr().out.splitlines()
    assert any(
        line.startswith("entity_refresh mgi failed") and line.endswith("header")
        for line in lines
    )
    assert any(line.startswith("entity_refresh rgd succeeded") for line in lines)


def test_interrupted_job_stays_running_and_the_next_run_resumes_it(
    use_runtime: InstallRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    """An infrastructure error leaves the job running; the next run reuses it."""
    use_runtime({"mgi": ConnectionResetError("database lost")})
    assert cli.main(["entity", "mgi"]) == 1
    first = capsys.readouterr().out
    assert "entity_refresh mgi running" in first
    job_id = first.split()[3]

    use_runtime({"mgi": GPI})
    assert cli.main(["entity", "mgi"]) == 0

    second = capsys.readouterr().out
    assert f"entity_refresh mgi succeeded {job_id}" in second


def test_unknown_source_creates_no_job(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unconfigured key is reported without creating a job."""
    use_runtime({})

    assert cli.main(["ontology", "chebi"]) == 1

    assert "No ontology source is configured" in capsys.readouterr().err
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 0


def test_cli_runs_retirement_for_catalog_without_configured_source(
    use_runtime: InstallRuntime,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`refresh entity` also retires an active catalog whose source was removed."""
    legacy = sources_with_entities(
        {"legacy": {"type": "https", "url": "https://example.org/legacy.gpi"}}
    )
    imports = EntityRefreshService(unit_of_work_factory, legacy)
    publish_catalog(imports, unit_of_work_factory, "legacy:1", source="legacy")
    only_mgi = sources_with_entities(
        {"mgi": {"type": "https", "url": "https://example.org/mgi.gpi"}}
    )
    use_runtime({"mgi": GPI}, only_mgi)

    assert cli.main(["entity"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("entity_retirement legacy succeeded") for line in lines)
    with session_factory() as session:
        action = AuditAction.ENTITY_RETIRED
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEventRecord)
                .where(AuditEventRecord.action == action)
            )
            == 1
        )


def test_cli_reports_when_no_sources_are_configured(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With nothing to refresh the CLI says so on stdout and exits 0."""
    use_runtime({}, sources_with_entities({}))

    assert cli.main(["entity"]) == 0

    assert "No entity sources are configured" in capsys.readouterr().out
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 0


def test_unknown_kind_is_a_usage_error() -> None:
    """An unknown refresh kind is a usage error (exit status 2)."""
    with pytest.raises(SystemExit) as raised:
        cli.main(["users"])

    assert raised.value.code == 2


def test_authorization_rejects_a_source_key(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Authorization has a single source, so naming one is a usage error."""
    use_runtime({"go-site": USERS_YAML})

    with pytest.raises(SystemExit) as raised:
        cli.main(["authorization", "go-site"])

    assert raised.value.code == 2
    assert "omit source_key" in capsys.readouterr().err
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 0


def test_cli_runs_an_annotation_refresh_in_process(
    use_runtime: InstallRuntime,
    seed_active_subjects: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`refresh annotation SOURCE` runs the GPAD refresh and reports success."""
    seed_active_subjects("UniProtKB:P12345")
    use_runtime({"mgi-gpad": gpad_bytes(gpad_row("UniProtKB:P12345"))})

    assert cli.main(["annotation", "mgi-gpad"]) == 0
    assert capsys.readouterr().out.startswith("annotation_refresh mgi-gpad succeeded")


def test_cli_rejects_a_sab_managed_source_without_a_job(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI exits with an error for a SAB-managed group and creates no job."""
    seed_group_import(
        session_factory,
        group_key="MGI",
        source_key="mgi-gpad",
        mode=AnnotationManagementMode.SAB_MANAGED,
    )
    use_runtime({})
    with session_factory() as session:
        jobs_before = session.scalar(select(func.count()).select_from(JobRecord))

    assert cli.main(["annotation", "mgi-gpad"]) == 1
    assert "SAB-managed" in capsys.readouterr().err
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(JobRecord)) == jobs_before
        )


def test_cli_refresh_all_says_when_every_annotation_group_is_sab_managed(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With every group SAB-managed, nothing starts and the message says why."""
    for group, source in (("MGI", "mgi-gpad"), ("RGD", "rgd-gpad")):
        seed_group_import(
            session_factory,
            group_key=group,
            source_key=source,
            mode=AnnotationManagementMode.SAB_MANAGED,
        )
    use_runtime({})
    with session_factory() as session:
        jobs_before = session.scalar(select(func.count()).select_from(JobRecord))

    assert cli.main(["annotation"]) == 0

    output = capsys.readouterr().out
    assert "SAB-managed" in output
    assert "not configured" not in output
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(JobRecord)) == jobs_before
        )
