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
    rule_files,
    sources_with_entities,
)
from seeding import active_snapshot, entity_identifiers, seed_entity_catalog
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.cli import refresh as cli
from standard_annotation_backend.domain.annotation_management import (
    AnnotationManagementMode,
)
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.auth import system_context
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    scan_lock_value,
    try_advisory_lock,
)
from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    JobRecord,
    SabeUserRecord,
)
from standard_annotation_backend.persistence.repositories.snapshots import (
    SnapshotRepository,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.refresh_start_service import (
    RefreshStartService,
)
from standard_annotation_backend.workers.runtime import create_job_executors

USERS_YAML = b"""
- accounts: {github: curator}
  authorizations:
    sabe: [{role: admin, scope: global}]
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
        def runtime(
            *, dispatch_follow_up: Callable[[Job], None] | None = None
        ) -> Iterator[SimpleNamespace]:
            yield SimpleNamespace(
                unit_of_work_factory=unit_of_work_factory,
                settings=SimpleNamespace(sources=sources),
                jobs=JobService(unit_of_work_factory),
                executors=create_job_executors(
                    engine=database_engine,
                    unit_of_work_factory=unit_of_work_factory,
                    sources=sources,
                    fetchers=fetchers,
                    dispatch_follow_up=dispatch_follow_up,
                ),
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
        assert session.scalar(select(SabeUserRecord.github_login)) == "curator"
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


def test_if_never_loaded_refreshes_a_database_without_authorization(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--if-never-loaded` refreshes authorization into a new database."""
    use_runtime({"go-site": USERS_YAML})

    assert cli.main(["authorization", "--if-never-loaded"]) == 0

    assert "authorization_refresh go-site succeeded" in capsys.readouterr().out
    with session_factory() as session:
        assert session.scalar(select(SabeUserRecord.github_login)) == "curator"


def test_if_never_loaded_skips_a_database_with_authorization(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Once authorization has been loaded, `--if-never-loaded` does nothing.

    It fetches nothing and starts no job, so a deploy that runs it every time does
    not depend on GitHub after the first.
    """
    use_runtime({"go-site": USERS_YAML})
    assert cli.main(["authorization"]) == 0
    capsys.readouterr()
    fetchers = use_runtime({"go-site": USERS_YAML})

    assert cli.main(["authorization", "--if-never-loaded"]) == 0

    assert fetchers.calls == []
    assert "Authorization is already loaded" in capsys.readouterr().out
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 1


@pytest.mark.parametrize("kind", ["rules", "ontology", "entity", "annotation"])
def test_if_never_loaded_is_a_usage_error_for_other_kinds(
    kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only authorization has a meaning for `--if-never-loaded`."""
    with pytest.raises(SystemExit) as exited:
        cli.main([kind, "--if-never-loaded"])

    assert exited.value.code == 2
    assert "--if-never-loaded applies only to authorization" in capsys.readouterr().err


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


def test_interrupted_job_stays_running_and_the_next_run_starts_it_again(
    use_runtime: InstallRuntime,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An infrastructure error leaves the job running; the next run reuses it.

    The error comes after the catalog is stored, while it is activated. The next
    run fetches the source again and finishes the same job.
    """

    def lose_database(*_args: object, **_kwargs: object) -> None:
        raise ConnectionResetError("database lost")

    use_runtime({"mgi": GPI})
    with monkeypatch.context() as patch:
        patch.setattr(SnapshotRepository, "activate", lose_database)
        assert cli.main(["entity", "mgi"]) == 1
    first = capsys.readouterr().out
    assert "entity_refresh mgi running" in first
    job_id = first.split()[3]

    fetchers = use_runtime({"mgi": GPI})
    assert cli.main(["entity", "mgi"]) == 0

    second = capsys.readouterr().out
    assert f"entity_refresh mgi succeeded {job_id}" in second
    assert fetchers.calls == ["mgi"]


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


def test_cli_retires_a_catalog_without_a_configured_source(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`refresh entity` retires an active catalog whose source was removed.

    The retirement is printed before the refresh jobs.
    """
    seed_entity_catalog(session_factory, ["ZFIN:1"], source_key="zfin")
    only_mgi = sources_with_entities(
        {"mgi": {"type": "https", "url": "https://example.org/mgi.gpi"}}
    )
    use_runtime({"mgi": GPI}, only_mgi)

    assert cli.main(["entity"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "entity zfin retired"
    assert lines[1].startswith("entity_refresh mgi succeeded")
    assert entity_identifiers(session_factory, "zfin") == set()


def test_cli_reports_a_retirement_when_no_sources_remain_configured(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Retiring the last catalogs is reported instead of "no sources configured"."""
    seed_entity_catalog(session_factory, ["ZFIN:1"], source_key="zfin")
    use_runtime({}, sources_with_entities({}))

    assert cli.main(["entity"]) == 0

    assert capsys.readouterr().out.splitlines() == ["entity zfin retired"]
    assert entity_identifiers(session_factory, "zfin") == set()


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


@pytest.mark.parametrize("kind", ["authorization", "rules"])
def test_single_source_kinds_reject_a_source_key(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    """Authorization and rules each have one source, so naming one is a usage error."""
    use_runtime({})

    with pytest.raises(SystemExit) as raised:
        cli.main([kind, "go-site"])

    assert raised.value.code == 2
    assert "omit source_key" in capsys.readouterr().err
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 0


def test_cli_refreshes_rules_in_process(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refreshing rules from the CLI activates a snapshot through a recorded job."""
    use_runtime({"go-site": rule_files()})

    assert cli.main(["rules"]) == 0

    assert "rules_refresh go-site succeeded" in capsys.readouterr().out
    assert active_snapshot(session_factory, SnapshotKind.RULES, "go-site") is not None


def test_cli_runs_an_annotation_refresh_in_process(
    use_runtime: InstallRuntime,
    seed_active_subjects: Callable[..., None],
    reference_data: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`refresh annotation SOURCE` runs the GPAD refresh and reports success."""
    seed_active_subjects("UniProtKB:P12345")
    use_runtime({"mgi-gpad": gpad_bytes(gpad_row("UniProtKB:P12345"))})

    assert cli.main(["annotation", "mgi-gpad"]) == 0
    assert capsys.readouterr().out.startswith("annotation_refresh mgi-gpad succeeded")


def test_cli_rejects_a_sabe_managed_source_without_a_job(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI exits with an error for a SABE-managed group and creates no job."""
    seed_group_import(
        session_factory,
        group_key="MGI",
        source_key="mgi-gpad",
        mode=AnnotationManagementMode.SABE_MANAGED,
    )
    use_runtime({})
    with session_factory() as session:
        jobs_before = session.scalar(select(func.count()).select_from(JobRecord))

    assert cli.main(["annotation", "mgi-gpad"]) == 1
    assert "SABE-managed" in capsys.readouterr().err
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(JobRecord)) == jobs_before
        )


def test_cli_rejects_a_source_whose_group_has_an_unfinished_cutover(
    use_runtime: InstallRuntime,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI exits with an error while the group's cutover is unfinished."""
    use_runtime({})
    RefreshStartService(
        unit_of_work_factory, TEST_SOURCES, lambda _job: None
    ).start_cutover(context=system_context("admin"), source_key="mgi-gpad")
    with session_factory() as session:
        jobs_before = session.scalar(select(func.count()).select_from(JobRecord))

    assert cli.main(["annotation", "mgi-gpad"]) == 1
    assert "unfinished GPAD" in capsys.readouterr().err
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(JobRecord)) == jobs_before
        )


def test_cli_refresh_all_says_when_every_annotation_group_is_sabe_managed(
    use_runtime: InstallRuntime,
    session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With every group SABE-managed, nothing starts and the message says why."""
    for group, source in (("MGI", "mgi-gpad"), ("RGD", "rgd-gpad")):
        seed_group_import(
            session_factory,
            group_key=group,
            source_key=source,
            mode=AnnotationManagementMode.SABE_MANAGED,
        )
    use_runtime({})
    with session_factory() as session:
        jobs_before = session.scalar(select(func.count()).select_from(JobRecord))

    assert cli.main(["annotation"]) == 0

    output = capsys.readouterr().out
    assert "their groups are SABE-managed or have an unfinished cutover" in output
    assert "not configured" not in output
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(JobRecord)) == jobs_before
        )


def test_cli_runs_the_queued_replacement_job_once_after_its_refreshes(
    use_runtime: InstallRuntime,
    seed_active_subjects: Callable[..., None],
    reference_data: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A publication's replacement job runs in-process and gets its own line."""
    seed_active_subjects("UniProtKB:P12345")
    use_runtime({"mgi-gpad": gpad_bytes(gpad_row("UniProtKB:P12345"))})

    assert cli.main(["annotation", "mgi-gpad"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("annotation_refresh mgi-gpad succeeded")
    assert len(lines) == 2
    assert lines[1].startswith("term_replacement ")
    assert " succeeded " in lines[1]


DEFERRED = (
    " deferred: another job of this type and scope is running; the next ontology "
    "activation or annotation publication sends it"
)


def test_a_busy_follow_up_is_reported_as_deferred_and_exits_zero(
    use_runtime: InstallRuntime,
    seed_active_subjects: Callable[..., None],
    reference_data: Callable[..., None],
    database_engine: Engine,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A follow-up whose scope another job holds stays queued and is not a failure."""
    seed_active_subjects("UniProtKB:P12345")
    use_runtime({"mgi-gpad": gpad_bytes(gpad_row("UniProtKB:P12345"))})

    with try_advisory_lock(
        database_engine,
        LockNamespace.ANNOTATION_SCAN,
        scan_lock_value(JobType.TERM_REPLACEMENT, "MGI"),
    ) as held:
        assert held
        status = cli.main(["annotation", "mgi-gpad"])

    assert status == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("annotation_refresh mgi-gpad succeeded")
    assert lines[1].startswith("term_replacement MGI queued ")
    assert lines[1].endswith(DEFERRED)
    assert "stopped before finishing" not in caplog.text


def test_a_busy_follow_up_does_not_hide_a_failed_refresh(
    use_runtime: InstallRuntime,
    seed_active_subjects: Callable[..., None],
    reference_data: Callable[..., None],
    database_engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI still exits nonzero when a refresh fails beside a deferred follow-up."""
    seed_active_subjects("UniProtKB:P12345")
    use_runtime(
        {
            "mgi-gpad": gpad_bytes(gpad_row("UniProtKB:P12345")),
            "rgd-gpad": RuntimeError("source down"),
        }
    )

    with try_advisory_lock(
        database_engine,
        LockNamespace.ANNOTATION_SCAN,
        scan_lock_value(JobType.TERM_REPLACEMENT, "MGI"),
    ) as held:
        assert held
        status = cli.main(["annotation"])

    assert status == 1
    lines = capsys.readouterr().out.splitlines()
    (rgd,) = [line for line in lines if " rgd-gpad " in line]
    assert " succeeded " not in rgd
    (replacement,) = [line for line in lines if line.startswith("term_replacement")]
    assert replacement.endswith(DEFERRED)
