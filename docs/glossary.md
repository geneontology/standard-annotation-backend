# Glossary

This glossary defines terms that have a specific meaning in SABE. Each entry says what
the term means here and points to where it is explained in full. It does not explain
the fields of a Standard Annotation; see the
[Standard Annotation schema documentation](https://geneontology.github.io/go-standard-annotation-schema/elements/)
for those. Generic technical terms, such as ontology, closure, or advisory lock, are not
defined here.

## GO concepts as SABE uses them

**Standard Annotation.** One GO annotation in the GO Standard Annotation schema: a
statement that a subject, such as a gene product, is associated with a GO term, with
evidence and references. SABE stores each annotation as a Standard Annotation payload
plus SABE metadata: an annotation ID, an owning group, a version, and a status. See
[`ARCHITECTURE.md`](../ARCHITECTURE.md#data-model-and-schema).

**Entity.** An annotation subject (`db_object_id`), such as a gene or protein, listed in
an entity catalog. SABE accepts a client-written annotation only when its subject is an
entity in an active catalog; otherwise the annotation has an `unknown_entity` issue.
See [Two stages](annotation-validation.md#two-stages).

**GPI and GPAD.** GO's file formats for gene product information and for annotations.
SABE reads GPI 2.0 files as entity catalogs and GPAD 2.0 files as group annotation
imports. One GPAD row can describe several independent annotations: each
pipe-separated (`|`) alternative in its with/from or annotation extension column is a
separate annotation. SABE splits such a row and stores each of those annotations on its
own, so one row in a GPAD file can become several annotations in SABE.

**go-site.** GO's metadata repository on GitHub. SABE reads `metadata/users.yaml` for
authorization and six other metadata files for rule data.

**GO Rule.** One of GO's official quality checks on annotations. SABE implements a
subset, each reported as its own issue type, such as `gorule_0000005`. See
[GO Rules](annotation-validation.md#go-rules).

**Evidence code.** The GO code, such as IPI or IEA, that GO Rules use to name evidence.
An annotation records evidence as an ECO term (`evidence_type`); SABE matches a rule's
code to the ECO term whose schema alias is that code.

## Annotations in SABE

**Owning group.** The group that owns an annotation (`owning_group_id`). It decides who
may change the annotation and whose GPAD file can replace it. It is SABE metadata, not a
Standard Annotation field. Not to be confused with a go-site `groups.yaml` shorthand,
which is what `assigned_by` holds.

**Group management mode.** Whether a group's annotations come from GPAD
(`gpad_imported`, the default) or are maintained in SABE (`sabe_managed`). A group
becomes `sabe_managed` only through a cutover, and the change is permanent.

**Annotation version.** Every create, update, or delete of an annotation writes a new
numbered version, and the annotation keeps its full history. Updates and deletes name
the version they expect to replace, and fail if it is no longer current. A delete is a
soft delete: the annotation's status becomes `deleted`.

**Change source.** The workflow recorded on each annotation version: `api` (a direct
request), `change_set`, `term_replacement`, `annotation_refresh`, or
`annotation_cutover`.

**Record origin.** How an annotation first entered SABE: `direct` (created in SABE) or
`import` (from a GPAD file).

**Change set.** A proposed create, update, or delete that someone with the admin role
reviews. It is `proposed` until it is `accepted` or `rejected`, and becomes `stale`
when the annotation it targets changes first. A change-set preview shows the result,
and any issues, before acceptance. See
[`ARCHITECTURE.md`](../ARCHITECTURE.md#change-sets).

**Comment.** A note attached to an annotation. Adding a comment does not create a new
annotation version.

**Duplicate and duplicate peer.** Two active annotations are duplicates when they agree
on the fields of SABE's duplicate policy and share at least one reference. An
annotation's duplicate peers are the other annotations it duplicates. See
[Duplicate Annotation Policy](../ARCHITECTURE.md#duplicate-annotation-policy).

## Reference data

**Reference data.** The external data SABE loads and checks annotations against:
authorization (users and grants), rule data, ontologies, and entity catalogs.

**Refresh kind.** One of the five kinds of data SABE refreshes from configured
sources: `authorization`, `rules`, `ontology`, `entity`, and `annotation` (GPAD). See
the [refresh guide](refresh-infrastructure.md).

**Source and source key.** A source is one configured place SABE fetches data from,
such as a GitHub file or an HTTPS URL. Each has a source key, such as `go` or `mgi`,
that names it in `config/sources.yaml`, in jobs, and in stored snapshots.

**Sources file.** `config/sources.yaml`, which lists every source of every refresh
kind. Each SABE process reads it once, at startup.

**Ontology catalog.** The configured ontologies and the identifier prefixes each owns.
SABE checks an identifier against the ontology that owns its prefix, and against no
other.

**Entity catalog.** The entities of one entity source, loaded from its GPI file. An
identifier can be active in only one catalog.

**Rule data.** The go-site metadata that GO Rules read: database cross-references,
GO_REFs, groups, retracted publications, evidence usage constraints, and extension
constraints.

**Snapshot.** One stored version of an ontology, the rule data, or an entity catalog.
Each source has at most one active snapshot, which validation reads. See
`persistence/repositories/snapshots.py` for the full contract.

**Predecessor.** The snapshot that the active snapshot replaced. It keeps its data, so
a reader that resolved it just before an activation can finish.

**Reader version.** The version of the code that read a source's document, recorded on
each snapshot and authorization refresh (`READER_VERSIONS` in `domain/snapshots.py`).
Raising it makes the next refresh read an unchanged document again.

**Unchanged.** A refresh is unchanged when the active snapshot, or for authorization
the latest refresh, already holds the same content: the same source type, locator,
checksum, and reader version, and for an ontology the same prefixes and closure
predicates. A new commit with identical bytes is unchanged. The job result records
`"unchanged": true`.

**Prune.** Delete the data of snapshots that are neither active nor a predecessor. Every
refresh job of a kind prunes that kind. The snapshot rows stay as a record of what was
loaded.

**Retire.** Deactivate the snapshot of a source removed from the sources file. Refreshing
every source of the ontology or entity kind retires removed sources; the kind's next
refresh job prunes their data.

**Reference data unavailable.** The `503 reference_data_unavailable` error. Creates,
updates, and change-set previews and acceptances return it, and QC report and term
replacement jobs fail with it, while any configured ontology or the rule data has no
active snapshot, or the active rule data was stored by an earlier version of SABE and
can no longer be read.

## Jobs and refreshes

**Job.** A durable record of one asynchronous operation: `queued`, then `running`, then
`succeeded` or `failed`. A Celery worker or the refresh CLI runs it. See the
[job guide](jobs.md).

**Refresh and refresh all.** A refresh is a job that fetches one source and applies it.
"Refresh all" starts a refresh for every configured source of a kind, and for
ontologies and entities also retires removed sources. At most one refresh job per
source is unfinished at a time.

**Fetch.** Download a source document, before the refresh's transaction opens.

**Store and activate.** What an ontology, rules, or entity refresh does with a changed
document: it stores a new snapshot, then makes it the source's active snapshot. Both
happen in the job's single transaction, together with recording the job's success. Its
progress reports the phases `storing` and `activating`.

**Staging and publication.** What a GPAD refresh or cutover does: it stages the file's
valid annotations, then publishes them, replacing the group's annotations, versions,
comments, and change sets. Both happen in the job's transaction. Its progress reports
the phases `staging` and `publishing`.

**Cutover.** A final GPAD import that moves a group to `sabe_managed`. It requires every
record in the file to be importable, and it starts only through the admin API.

**Term replacement.** A job that replaces obsolete terms and alternative IDs in active
annotations with their current terms, wherever the result passes validation. Every
ontology activation and every GPAD publication queues one. It is an annotation scan
job. See
[Automatic term replacement](annotation-validation.md#automatic-term-replacement).

**Replacement chain.** The sequence of replacements that term replacement follows from
an obsolete term or alternative ID: from an alternative ID to its primary term, or from
an obsolete term to its only `replaced_by` term, until it reaches a current term. See
[Automatic term replacement](annotation-validation.md#automatic-term-replacement).

**Follow-up job.** A job that a refresh queues in its own transaction, such as a term
replacement job. The runner sends it to a worker after the refresh commits.

**Annotation scan job.** A job that reads every active annotation of one owning
group, or of all groups, in batches. Term replacement and QC reports are annotation
scan jobs. See [Annotation scan jobs](jobs.md#annotation-scan-jobs).

**QC report and finding.** A QC report is a job that validates every active annotation
of one group or all groups; it is an annotation scan job. A finding is one
annotation's list of issues in a report. See
[Reports](annotation-validation.md#reports).

**Failure code.** The machine-readable reason a job failed, such as `invalid_document`
or `catalog_collision`, stored in the job's progress. Every job type can fail with
`dispatch_failed`; annotation scan jobs can also fail with `abandoned`. See
[The job record](jobs.md#the-job-record).

## Validation

**Structural and semantic validation.** Structural validation checks an annotation
against the Standard Annotation schema. Semantic validation checks a structurally valid
annotation against reference data, other annotations, and GO Rules. See
[Two stages](annotation-validation.md#two-stages).

**Issue and issue type.** An issue is one problem validation found, with a location, a
message, a type, and sometimes details. The issue type is its stable name, such as
`unknown_term`, `duplicate_annotation`, or `gorule_0000005`.

**Blocks writes.** A semantic rule that blocks writes rejects any create or update that
has its issue. A rule that does not block writes runs only in QC reports.

**Checked ancestors.** The GO terms that GO Rules test other terms against, such as the
three GO roots. Before the rules run, SABE looks up which checked ancestors each term
falls under.

## Access

**Role.** What an identity may do: `read`, `edit`, or `admin`.

**Scope.** Which annotations a role applies to: `self` (annotations the user created),
`group` (annotations of one owning group), or `global` (every annotation). See
[Authorization Model](../ARCHITECTURE.md#authorization-model).

**Grant.** One role and scope assigned to a user, for `self` and `group` scopes in a
named group. Grants come from go-site `users.yaml` through authorization refreshes.

**Bearer token.** The credential every API request carries. A user creates named,
expiring tokens in the token management interface after signing in with GitHub. Each
token is bound to one of the user's grants: a `self` or `group` grant gives a token
restricted to that group, which also becomes the default owning group of annotations
it creates. See [Bearer Token Context](../ARCHITECTURE.md#bearer-token-context).

**Actor.** The identity recorded as having performed an action: a user, or a process
such as `scheduler` or `cli`.

**Audit event.** A durable record of one action: what was done, by which actor, and to
which annotation, job, or change set.
