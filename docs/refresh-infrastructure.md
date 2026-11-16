# Refresh infrastructure guide

This guide is for developers changing or debugging reference data refreshes.
`ARCHITECTURE.md` describes the design at a high level, and docstrings describe
each piece. This guide connects the two. It follows a refresh from the moment
something starts it, through the job that runs it, to the rows it writes in
PostgreSQL.

SABE refreshes five kinds of reference data:

| Kind | Source format | Configured in `config/sources.yaml` | Source keys |
|---|---|---|---|
| `authorization` | go-site `users.yaml` | `authorization` (one entry) | always `go-site` |
| `rules` | six go-site metadata files read at one commit | `rules` (one entry: `repository` and `ref`) | always `go-site` |
| `ontology` | OBO | `ontologies` | each entry's key, such as `go` |
| `entity` | GPI 2.0 | `entities` | each entry's key, such as `mgi` |
| `annotation` | GPAD 2.0 | `annotations` (each entry names the `group` it replaces) | each entry's key, such as `mgi` |

The annotation kind has two job types, `annotation_refresh` and
`annotation_cutover`, both served by `AnnotationRefreshService`. A cutover is a
final replacement that moves the group to SABE management. It starts only from
the admin API, through `RefreshStartService.start_cutover`.

## The whole picture

Every refresh, however it starts, goes through the same two services:
`RefreshStartService` creates jobs, and `RefreshRunner` runs them. The runner
handles the steps every kind shares and calls the kind's refresh service for the
steps that differ. Each refresh service implements the `RefreshKind` protocol.

```mermaid
flowchart LR
    subgraph Triggers
        beat["Celery Beat<br/>SABE_*_REFRESH_CRON"]
        api["POST /admin/*-refreshes"]
        cli["just refresh KIND [SOURCE]"]
    end

    yaml[("config/sources.yaml")] --> settings["Settings.sources<br/>(RefreshSources)"]

    beat --> sched["sabe.refresh.schedule(kind)"]
    sched --> start["RefreshStartService.start"]
    api --> start
    cli --> start
    settings --> start

    start -->|"commit job rows"| jobs[("job, audit_event")]
    start -->|"Celery dispatch"| run["sabe.refresh.run(job_id)"]
    start -->|"CLI dispatch"| inproc["run in the CLI process"]

    run --> runner["RefreshRunner"]
    inproc --> runner
    settings --> runner
    runner --> fetch["SourceFetchers<br/>(GitHub or HTTPS)"]
    runner --> services["AuthorizationRefreshService<br/>RulesRefreshService<br/>OntologyRefreshService<br/>EntityRefreshService<br/>AnnotationRefreshService"]
    services --> repos["Repositories"]
    repos --> tables[("reference_snapshot + kind tables;<br/>auth and annotation tables")]
    runner -->|"progress, result, status"| jobs
```

Two rules shape everything else:

- **Jobs store only a source key.** A job's parameters are always
  `{"source_key": "<key>"}`. The runner looks the source up in the sources file
  when the job runs, not when it was created. A source removed from the file
  before its job runs fails with `unknown_source` and is never fetched.
- **Everything is safe to repeat.** Celery can deliver a task message more than
  once. A start can find a job already queued. A job runs in one transaction, so
  a job that ran again after its worker stopped starts from the beginning; a
  finished job is never run again.

## Inputs: `config/sources.yaml`

`Settings` reads the file when it is constructed (`SABE_SOURCES_FILE`, default
`config/sources.yaml`). An invalid file stops the API, worker, scheduler, and CLI
at startup. The models live in `refresh/sources.py`:

```mermaid
flowchart LR
    file[("config/sources.yaml")] -->|"load_sources_file"| model["SourcesFile<br/>(Pydantic validation)"]
    model --> lookup["RefreshSources<br/>sources(kind), keys(kind), source(kind, key),<br/>ontology_catalog()"]
    lookup --> gh["GitHubSource<br/>repository, ref, path"]
    lookup --> https["HttpsSource<br/>url"]
    gh -->|"GitHubSourceFetcher:<br/>resolve ref, download raw file"| doc["SourceDocument<br/>content + provenance"]
    https -->|"HttpsSourceFetcher:<br/>download, follow HTTPS redirects"| doc
    lookup --> rules["GitHubRulesSource<br/>repository, ref"]
    rules -->|"GitHubSourceFetcher.fetch_files:<br/>resolve ref once, download six files, pack them"| doc
```

`ontology_catalog()` returns the configured ontologies and the identifier prefixes
each owns. Validation, term replacement, QC reports, and the rules refresh's
extension-namespace warnings all use it to decide which ontology an identifier
belongs to.

A `SourceDocument` carries the downloaded bytes and their provenance:
`source_type`, `source_locator` (`github:<repository>:<path>`, `github:<repository>`
for the rules source, or the URL), `source_revision` (the resolved commit, or `None`
for HTTPS), `source_checksum` (SHA-256 of the downloaded bytes), and `fetched_at`.
Every kind stores these fields on the record it creates and uses them to decide
whether a document is already active. The resolved revision is not part of that
decision: a new commit with identical bytes is the same content. The rules source
is six files read at one commit; `pack_source_files` combines them into one
document's content, so they share one provenance and one checksum, and the rules
service separates them again with `unpack_source_files`.

## Starting a refresh

| Trigger | Entry point | `requested_by` | How jobs are dispatched |
|---|---|---|---|
| Schedule | Beat entries `authorization-refresh`, `rules-refresh`, `ontology-refresh`, `entity-refresh`, `annotation-refresh` call `sabe.refresh.schedule(kind)` in `workers/tasks.py` | `scheduler` | Celery (`tasks.dispatch_refresh_job`) |
| API | `api/routes/admin.py` | the caller's actor ID | Celery (`tasks.dispatch_refresh_job`) |
| CLI | `cli/refresh.py` (`just refresh`) | `cli` | in the CLI process, one job at a time |

All three triggers call `RefreshStartService.start(kind, requested_by=..., source_key=...)`.
A `source_key` refreshes one source; `None` refreshes every configured source of the
kind. The authorization and rules routes, and the CLI for those kinds, never pass a key,
because each kind has only one source. For annotations, "refresh all" leaves out groups
that are SABE-managed or have an unfinished cutover, and naming a SABE-managed group's
source raises `GroupSabeManagedError` (`group_sabe_managed`, HTTP 409) before any job is
created. Cutovers use a separate entry point, `RefreshStartService.start_cutover`, which
the `POST /admin/annotation-cutovers` route calls. The schedule and CLI never start
cutovers.

```mermaid
sequenceDiagram
    participant T as Trigger
    participant S as RefreshStartService
    participant DB as PostgreSQL
    participant D as Dispatcher

    T->>S: start(kind, requested_by, source_key)
    S->>S: unknown source_key? raise UnknownSourceError (no job created)
    S->>DB: take refresh start lock (transaction)
    Note over S,DB: ontology and entity "refresh all" retires active snapshots of unconfigured sources (no job)
    loop each target source
        S->>DB: find queued or running job for (job type, source key)
        alt none found
            S->>DB: insert queued job + audit job.queued
        end
    end
    S->>DB: commit (releases the lock)
    loop each queued or running job
        S->>D: dispatch(job)
        alt dispatch raises and this call created the job
            S->>DB: fail job: dispatch_failed
        end
    end
    S-->>T: RefreshStart(jobs, retired)
```

Points worth knowing when something looks wrong:

- **Reuse.** A start never creates a second unfinished job for the same source.
  It returns the existing one and dispatches it again. That is how a job whose
  worker disappeared runs again: any later start sends it again.
- **Commit before dispatch.** A worker must be able to read the job it is sent.
- **Dispatch failures.** Only a job created by this call is marked failed
  (`dispatch_failed`). A reused job may already be in the broker, so it is left
  as it is.
- **Routing.** `tasks.dispatch_refresh_job` sends every job to `sabe.refresh.run`.
  Celery messages carry only the job ID.
- **Retirement.** For ontologies and entities, "refresh all" retires, in the start
  transaction, each source that has an active snapshot but is no longer configured and
  has no unfinished refresh job. It writes an `ontology.retired` or `entity.retired`
  audit event for each and creates no job. The retired snapshot stops being read at
  once, and the kind's next refresh job deletes its data. The CLI prints
  `<kind> <key> retired` for each. After removing an ontology from
  `config/sources.yaml`, restart the web and worker processes with the new file
  before refreshing all ontologies (see the Ontology note below).
- **One GPAD job per group.** A cutover request while a refresh of the group is
  unfinished, or a one-source refresh while a cutover is unfinished, fails with
  `annotation_job_in_progress` (HTTP 409); refresh-all skips the group.

## Running a job

`RefreshRunner.run(job_id)` in `refresh/runner.py` is the same for every kind.
The Celery task `sabe.refresh.run` and the CLI both call it.

```mermaid
flowchart TD
    A["take job execution lock<br/>(try-lock on a dedicated connection)"] -->|held elsewhere| Z1["return: another process runs this job"]
    A -->|acquired| B["jobs.start: queued to running<br/>(audit job.started)"]
    B --> C{"job already<br/>succeeded or failed?"}
    C -->|yes| Z2["return"]
    C -->|no| D{"known job type and<br/>valid source_key?"}
    D -->|no| F1["fail: invalid_parameters"]
    D -->|yes| G{"source still in<br/>sources.yaml?"}
    G -->|no| F2["fail: unknown_source"]
    G -->|yes| I["progress phase: fetching<br/>SourceFetchers.fetch"]
    I --> K["one transaction: kind.apply(unit_of_work, job, document, report),<br/>then record success with final progress; commit"]
    K --> L["send follow-up jobs (term replacement)"]
```

Any exception raised after the job has started is classified:

| Error | Outcome |
|---|---|
| `SourceError` (from fetching or decoding) | job fails with its code: `timeout`, `http_status`, `source_error`, `invalid_gzip`, `invalid_utf8` |
| `UnknownSourceError` | job fails with `unknown_source` |
| `TerminalRefreshError` (raised by a kind) | job fails with its code and `failure_details` |
| anything else | re-raised; the job's transaction rolls back and the job stays `running` |

The job's transaction covers `apply` and the recording of success. Fetching
happens before it opens, and follow-up jobs are sent after it commits. If
sending one fails, the runner logs `Follow-up job dispatch failed` and the
follow-up job stays `queued`.

The runner records a failed job through `JobService.fail_refresh`: the status,
the failure progress
`{"phase": "failed", "failure_code": ..., "failure_details"?: ...}`, and the
`job.failed` audit event commit in one transaction.

A re-raised error means "try again later". Celery's wrapper
(`_retry_safely` in `workers/tasks.py`) logs only the exception type and retries
after 30 seconds. The CLI logs it and reports the job as still `running`. A
re-raised error rolls back everything the attempt did, and the next delivery or
start runs the job from the beginning.

### The job record over time

```mermaid
stateDiagram-v2
    [*] --> queued: start (audit job.queued)
    queued --> running: runner starts it (audit job.started)
    queued --> failed: dispatch_failed
    running --> running: phases fetching, applying (and kind-specific phases)
    running --> succeeded: phase completed (audit job.succeeded)
    running --> failed: phase failed + failure_code (audit job.failed)
    succeeded --> [*]
    failed --> [*]
```

Kinds may report their own phases inside `applying`. Snapshot kinds (ontology,
rules, and entity) report `storing` and then `activating` when they store a new
snapshot, and annotation refreshes report `staging` and then `publishing`. When
nothing was applied because the document was already active, `apply` returns an
outcome with `unchanged` set. The runner alone records it: completed progress and
the job result of every refresh job include `"unchanged"` (`true` or `false`).

Progress is written in its own transactions while the job's transaction is open,
so clients can follow a long job. Job state changes lock the job row with
`FOR NO KEY UPDATE`, which never waits for the job's own rows that refer to it.

## What each kind does

Each kind's refresh service implements the `RefreshKind` protocol in
`refresh/runner.py`. Read the rows below side by side: the steps are the same,
and only their contents differ. Recognizing an already active document is part
of `apply`.

| Step | Authorization (`AuthorizationRefreshService`) | Rules (`RulesRefreshService`) | Ontology (`OntologyRefreshService`) | Entity (`EntityRefreshService`) | Annotation (`AnnotationRefreshService`) |
|---|---|---|---|---|---|
| unchanged check (inside `apply`) | latest `authorization_refresh` row has the same content (type, locator, checksum) and reader version | active `rules` snapshot has the same content and reader version | active `ontology` snapshot has the same content and reader version, and the same prefixes and closure predicates | active `entity` snapshot of the source has the same content and reader version | ordinary refreshes only (a cutover never skips): the group is `gpad_imported`, its last import has the same checksum and schema package version, and the group has no local changes |
| `apply` | decode UTF-8, validate `users.yaml`, replace users and grants | unpack, decode, and validate the six files, store and activate a snapshot | parse OBO, store and activate a snapshot, queue term replacement | decode, parse GPI, store a snapshot, check no other source's active catalog lists an identifier (under the entity activation lock), activate | refuse a SABE-managed group, decode, parse GPAD, stage, publish (replacing the group's annotations), queue term replacement |
| then | — | prune rules snapshots | prune ontology snapshots | prune entity snapshots | — |
| Tables written | `sabe_user`, `sabe_group`, `authorization_assignment`, `authorization_refresh` | `reference_snapshot`, `rules_snapshot_detail`, `rule_data`, `retracted_publication` | `reference_snapshot`, `ontology_snapshot_detail`, `ontology_term`, `ontology_alt_id`, `ontology_closure` | `reference_snapshot`, `entity_snapshot_detail`, `entity_record` | `annotation_staging` (and child tables), `annotation_import`, `group_annotation_management`, and the group's annotation rows |
| Audit action | `authorization.refreshed` | `rules.refreshed` | `ontology.refreshed` (`ontology.retired` at refresh-all start) | `entity.refreshed` (`entity.retired` at refresh-all start) | `annotation_refresh.published` or `annotation_cutover.published` |
| Kind-specific failure codes | `invalid_document` | `invalid_document` | `invalid_document` | `header`, `row_validation`, `catalog_collision` | `header`, `no_valid_annotations`, `cutover_rejected`, `group_sabe_managed` |

The rules, ontology, and entity kinds prune their kind's snapshots whether or not
they stored a new one. For each source, the active snapshot and the one it replaced
keep their data. Every refresh job of the kind deletes the data of the rest, and
skips any snapshot a transaction is still reading until a later job. A retired
source keeps none. Snapshot rows stay as provenance after their data is deleted.

Some details that matter when debugging:

- **Authorization.** The service compares the document with the most recent
  refresh and replaces users and grants only when it differs. Refreshes never
  overlap, because at most one authorization refresh job is unfinished at a time.
- **Rules.** One commit's six files make one snapshot, so a snapshot never mixes
  revisions. `apply` validates all six before it stores anything, and failure
  details locate each issue by file path. The unchanged check compares the files,
  not the commit, because most go-site commits change none of them. The result's
  `unrecognized_extension_namespaces`, with one warning each, lists
  `extensions-constraints.yaml` namespaces that are neither a configured ontology
  prefix nor a `db-xrefs.yaml` database. It is recomputed on every run against the
  current configuration, including unchanged ones.
- **Ontology.** Removing an ontology from `config/sources.yaml` stops its prefix
  from being checked once processes reload their configuration, and the next
  refresh of every ontology retires its snapshot. Each process reads the file once,
  when it starts, and writes need every ontology the process lists as configured. A
  web process that still lists a retired ontology therefore returns
  `503 reference_data_unavailable` for writes until it restarts. Restart the web
  and worker processes with the new file first, and refresh all ontologies after.
- **Entity.** The collision check runs under the entity activation lock, so two
  sources cannot both activate the same new identifier. Removing a source from the
  configuration retires its catalog on the next refresh of every entity source.
- **Annotation.** Publication replaces the target group's annotations, change
  sets, comments, and related audit events, and cannot be undone. Staging and
  publication happen in the job's transaction. Publication takes the exclusive
  global annotation-write lock after staging and holds it until the job commits,
  so annotation writes wait only for publication. Rows that cannot be read or
  fail structural validation are rejected and reported; an ordinary refresh
  publishes the rest, and a cutover fails on any rejection.

## Term replacement jobs

A `term_replacement` job replaces obsolete terms and alternative IDs in active
annotations (see `docs/annotation-validation.md`). It is not a refresh kind: it
fetches nothing, and `TermReplacementService` runs it instead of `RefreshRunner`.

- **Triggers.** Every ontology activation queues a job for all groups
  (parameters `{}`), and every annotation publication queues one for that group
  (`{"group_key": ...}`). The job is created in the activation's or publication's
  transaction, with the triggering job's requester, and is sent to
  `sabe.term_replacement.run` after that transaction commits.
- **Coalescing.** A trigger joins the scope's queued job if there is one; a running
  job is never joined, because it may have read its facts before the change. If
  sending a job fails, it stays queued, and the next trigger for its scope joins it
  and sends it again.
- **Running.** The job holds its own lock and its scope's lock; if another job of
  the same scope is running, the task is retried and the job stays queued. It reads
  active annotations in `annotation_id` order, 500 at a time, without locks, and
  writes each changed annotation in its own transaction through the normal update
  path. The result counts annotations scanned, replaced, skipped because of issues,
  and skipped as stale, and lists the snapshot IDs read.
- **Failures.** `invalid_parameters` for parameters other than `{}` or
  `{"group_key": ...}`, and `reference_data_unavailable` when a configured ontology
  or the go-site rule data has no active snapshot, or the active rule data was stored
  by an earlier release and cannot be read until a rules refresh.
- **The CLI.** `just refresh` runs the replacement jobs its refreshes queued once,
  in its own process, after the refresh jobs finish, and prints a line for each.

## QC report jobs

An `annotation_qc_report` job validates every active annotation in its scope and stores
findings and counts (see `docs/annotation-validation.md`). Like term replacement, it is
not a refresh kind: `QcReportRunner` runs it instead of `RefreshRunner`.

- **Triggers.** `POST /reports/annotation-qc` (admin role) and the Beat entry
  `annotation-qc-report`, which calls `sabe.qc_report.schedule` on `SABE_QC_REPORT_CRON`
  to start an all-groups report. Both join the scope's queued report if there is one,
  and send the job to `sabe.qc_report.run` after it commits. If sending fails, the job
  stays queued until the next start of its scope.
- **Running.** The job holds its own lock and its scope's lock; if another report of the
  same scope is running, the task is retried and the job stays queued. It first deletes
  rows left by an earlier attempt and checks that reference data is available. It then
  reads active annotations in `annotation_id` order, 500 at a time, without locks, and
  commits each batch's findings and counts. Reference data is resolved per batch, and
  the result lists every distinct set used. Marking the job succeeded and deleting the
  findings of earlier reports of its scope commit together. Reports of a scope are
  ordered by completion time, then job ID.
- **Failures.** `invalid_parameters`, and `reference_data_unavailable`. A failed report
  keeps no findings or counts.
- **The CLI.** `just refresh` does not run QC reports.

## Locks

| Lock | Kind of lock | Held by | Prevents |
|---|---|---|---|
| Refresh start lock | transaction advisory lock | `RefreshStartService.start` | two starts both creating a job for the same source |
| Job execution lock | session try-lock, one per job | `RefreshRunner.run`, `TermReplacementService.run`, `QcReportRunner.run` | two processes running one job after a duplicate delivery |
| Entity activation lock | transaction advisory lock | entity `apply`, from the collision check through commit | two sources activating the same new identifier |
| Snapshot read locks | `FOR KEY SHARE` row locks on `reference_snapshot` | `SnapshotRepository.resolve` and `require_readable` | pruning a snapshot a transaction is reading (pruning skips locked snapshots) |
| Annotation system-update lock | exclusive global annotation-write lock | annotation publication | ordinary annotation writes during group replacement |
| Term replacement start lock | transaction advisory lock | `queue_term_replacement` | two triggers both creating a queued job |
| Term replacement scope lock | session try-lock, one per scope | `TermReplacementService.run` | two jobs of one scope running at once |
| QC report start lock | transaction advisory lock | `queue_qc_report` | two starts both creating a queued report |
| QC report scope lock | session try-lock, one per scope | `QcReportRunner.run` | two reports of one scope running at once |

The try-locks never wait. A process that cannot take one either returns (job
lock) or raises so the task is retried later: the term replacement and QC report
scope locks (`TermReplacementBusyError` and `QcReportBusyError`).

## Where to start

| Symptom or task | Look at |
|---|---|
| A job stays `running` | worker logs for `Worker task unavailable` (Celery retries) or `Refresh job stopped before finishing` (CLI); a refresh that stopped committed nothing, so start the same refresh again to dispatch it, and it runs from the beginning. A running term replacement or QC report job resumes only when Celery redelivers or retries its task; a QC report then starts again from empty rows |
| A term replacement or QC report job stays `queued` | worker logs for `Follow-up job dispatch failed` or `QC report dispatch failed` (the next trigger for its scope sends it again), or another job of its scope holding the scope lock (the task retries every 30 seconds) |
| A job failed | its `progress.failure_code` and `failure_details` through `GET /admin/jobs/{job_id}`; the table of classified errors above |
| `unknown_source` | the job's `source_key` against `config/sources.yaml` as loaded by the running process (settings are cached per process) |
| A refresh did nothing | completed progress with `"unchanged": true` (also in the job result): the source document is already active |
| A scheduled refresh never ran | `workers/celery_app.py` Beat entries and the `SABE_*_REFRESH_CRON` settings |
| Reference data needs reloading | a migration that clears snapshots, or a raised `READER_VERSIONS` entry: run `just refresh` for the kind (writes return `503 reference_data_unavailable` until every configured ontology and the rules have snapshots). To reload everything, refresh rules, then entity, then ontology: an ontology refresh queues a term replacement job, which needs the rules and every ontology loaded, and saves a replacement only for an annotation whose subject is in an entity catalog |
| Adding a source to an existing kind | `config/sources.yaml` only; no code change |
| Adding a source type | `refresh/sources.py` (model) and `refresh/fetchers.py` (fetcher) |
| Adding a refresh kind | a refresh service implementing `RefreshKind` in `services/`, its job type and audit action in `domain/`, wiring in `workers/runtime.py`, a section in `config/sources.yaml`, a Beat entry, and an admin route |

## Tests

| Area | Tests |
|---|---|
| Sources file and fetchers | `tests/unit/test_sources_file.py`, `tests/unit/test_source_fetchers.py` |
| go-site rule files | `tests/unit/test_go_site_rule_files.py`, `tests/unit/test_rule_data.py` |
| Shared lifecycle, run for every kind | `tests/integration/test_refresh_runner.py` (the `CASES` table) |
| Starting, reuse, dispatch, and concurrent starts | `tests/integration/test_refresh_start_service.py` |
| Kind-specific behavior | `tests/integration/test_*_refresh_jobs.py`, `test_*_refresh_service.py`, `test_*_refresh_concurrency.py`, `test_reference_snapshots.py`, `test_reference_data_retirement.py`, `test_users_yaml_sync.py` |
| Entry points | `tests/integration/test_admin_api.py`, `tests/integration/test_refresh_cli.py`, `tests/unit/test_worker_task_safety.py`, `tests/unit/test_celery_app.py` |

`tests/integration/refresh_helpers.py` provides `FakeFetchers`, which returns
fixed documents instead of using the network, and `build_runner`, which builds
the production runner and kinds around them.
