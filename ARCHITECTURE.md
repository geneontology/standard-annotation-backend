# New Standard Annotation Backend

This document proposes an architecture for a stable, initial version of the new Standard Annotation Backend (SAB). It is intended as a starting point for technical review and team discussion, not as a final implementation plan.

## Resources

* A human-readable definition of standard annotations can be found here: [https://geneontology.org/docs/go-annotations/\#standard-go-annotations](https://geneontology.org/docs/go-annotations/#standard-go-annotations)  
* A standard annotation is essentially one line from a GPAD file (along with the associated line from a GPI file). The format of these files is specified here: [https://github.com/geneontology/go-annotation/blob/master/specs/gpad-gpi-2-0.md](https://github.com/geneontology/go-annotation/blob/master/specs/gpad-gpi-2-0.md)  
* New SAB project issue: [https://github.com/geneontology/project-management/issues/130](https://github.com/geneontology/project-management/issues/130) (contains links to a few older brainstorming documents)

## Scope and Goals

SAB is a *de novo* replacement for GO's current standard annotation storage and access patterns. It is intended to be a long-term maintainable backend service that owns Standard Annotation records, exposes them through a stable programmatic API, supports reviewable change-set and direct mutation operations, and can derive GPAD/GPI-style outputs from its database.

SAB is authoritative for Standard Annotation records and their lifecycle (creation, modification, deletion). It stores references to external identifiers, ontology terms, evidence terms, publications, and gene products, but it is explicitly not authoritative for those external resources. External metadata may be cached for display, search, validation, or export support, but those caches are not the source of truth.

Primary goals:

* Define a LinkML-based core Standard Annotation model.  
* Provide a conventional JSON-over-HTTP API documented with [OpenAPI](https://www.openapis.org/).  
* Store annotations in PostgreSQL using a relational-first model with JSONB snapshots.  
* Support both direct [CRUD](https://en.wikipedia.org/wiki/Create,_read,_update_and_delete#RESTful_APIs) writes and reviewable [JSON Patch](https://jsonpatch.com/)\-based change sets.  
* Preserve immutable annotation versions and first-class audit events.  
* Support API-triggered asynchronous GPAD/GPI import and export jobs.  
* Support semantic validation and reporting.  
* Prevent new duplicate annotations once an agreed duplicate definition is in place.  
* Use GitHub OAuth as an identity provider and a simple role/scope model for authorization.  
* Fit into GO's AWS/container deployment ecosystem.

Non-goals:

* Redesigning the Standard Annotation Editor (SAE) or optimizing SAB around the current editor/backend contract.  
* Replacing ontology services, publication systems, gene product databases, or external identifier authorities.  
* Treating GPAD/GPI files as the long-term source of truth.  
* Automatically applying stale concurrent change sets.  
* Encoding application internals such as jobs, audit logs, and authorization rules in LinkML.

## Architecture Overview

The recommended architecture is a conservative Python service stack:

* [FastAPI](https://fastapi.tiangolo.com/) for the HTTP API and OpenAPI documentation.  
* [Pydantic](https://pydantic.dev/docs/validation/latest/get-started/) for API request and response models.  
* [LinkML](https://linkml.io/linkml/) for the canonical Standard Annotation domain schema.  
* [SQLAlchemy](https://www.sqlalchemy.org/) and [Alembic](https://alembic.sqlalchemy.org/en/latest/) for database models and migrations.  
* [PostgreSQL](https://www.postgresql.org/) as the authoritative database.  
* JSONB columns for full annotation snapshots and selected flexible metadata.  
* [Celery](https://docs.celeryq.dev/en/stable/index.html) workers with Redis for asynchronous import/export, ontology loading, and reporting jobs.  
* S3-compatible object storage for import files, export files, and large job artifacts.

This approach keeps the operational and application stack based on widely used and maintained technologies, using LinkML where it has direct value.

## Data Model and Schema

### Schema Definition and Usage

SAB should use LinkML as the schema definition language for the core Standard Annotation data model. The LinkML schema should describe what a valid Standard Annotation is: required fields, optional fields, cardinality, identifier formats, controlled values, documentation, and schema versioning.

The LinkML schema should stay focused on the domain objects. It should not model SAB internals such as users, jobs, audit events, change-set state, database projections, or authorization. Those are application concerns.

The Standard Annotation LinkML schema should be developed in the standalone [`geneontology/go-standard-annotation-schema`](https://github.com/geneontology/go-standard-annotation-schema) repository outside the SAB codebase. SAB should consume Python-based schema artifacts, including Pydantic models, from the versioned PyPI package published by that repository. SAB should consume non-Python artifacts, such as JSON Schema definitions, from GitHub Release assets published by that same repository. SAB should pin the package and release artifact versions it uses rather than treating the SAB repository as the authoritative home for the schema.

Generated LinkML artifacts should be used by SAB where appropriate:

* JSON Schema or related validation artifacts for the canonical annotation payload.  
* Pydantic models for request/response definitions.  
* Documentation outputs for developers and collaborators.  
* SQLAlchemy or SQL DDL output as a design aid or starting point, not as the authoritative persistence model.

The database models should be hand-authored using SQLAlchemy and Alembic, informed by the LinkML model but not directly derived from it. The database needs to represent application concerns such as versioning, change sets, audit events, jobs, authorization, indexes, and migrations; those concerns do not belong in the core Standard Annotation LinkML schema.

### Identifiers

A Standard Annotation record should store references to external entities as identifiers, usually CURIEs or provider-specific IDs. Examples include gene product IDs, GO terms, ECO terms, and references such as PMIDs. SAB may cache labels or lookup metadata, but the annotation model itself should remain identifier reference-based.

Existing annotations do not have stable IDs, so SAB must assign them. SAB should use backend-generated UUIDs as annotation identifiers.

### Validation

SAB should synchronously enforce structural validity for normal writes: required fields, field types, cardinality, allowed enum-like values, and other LinkML/Pydantic constraints. SAB should not store structurally invalid annotations through normal CRUD, change-set acceptance, or import workflows. Draft or quarantine storage for invalid annotations is not part of the initial design.

Referential validation against external resources is not an initial design consideration, but it could be added later without major changes to the architecture being proposed now.

## Data Store and Versioning

SAB should use PostgreSQL as its authoritative data store. The schema should be relational-first. JSONB should be used judiciously for full annotation snapshots and flexible metadata, rather than as the only representation of an annotation.

The main `annotation` table should represent the current state of each annotation. Stable, commonly queried fields should be first-class columns with indexes: annotation ID, subject/gene product ID, object/GO term ID, evidence code, assigned-by value, status, current version, created/updated timestamps, and deletion state. More complex or repeated structures can use child tables where they are important for query or export behavior.

Each mutation should create an immutable annotation version. A lightweight `annotation_version` table should store the full LinkML-shaped annotation snapshot, version number, actor, timestamp, and the source of the change. The main `annotation` table stores the current version and query projection; historical versions are stored separately in `annotation_version`.

A proposed table sketch:

```
annotation
  current annotation state, ownership/group fields, indexed query fields, 
  soft-delete status; multivalued annotation fields needed for querying may be
  stored in child tables

annotation_version
  immutable full annotation snapshots, one row per accepted version

annotation_comment
  user-authored annotation comments with the annotation version observed at
  creation time, author, timestamps, body, and optional soft-delete metadata

change_set
  proposed create/update/delete annotation changes, review state, optional
  base annotation version, preview metadata

audit_event
  append-only operational history across annotations, change sets, jobs, auth
  syncs, and admin actions

job
  records for import/export jobs and other asynchronous work

ontology_metadata
  loaded ontology versions, source metadata

ontology_closure
  precomputed ontology closure rows for approved predicates, with subject term,
  predicate, object term, and depth; subject is the more specific matched term,
  object is the broader queried term

user
  normalized identities, including GitHub login and optional profile metadata

group                                                                                                                                                      
  normalized group identifiers referenced by SAB authorization and annotation
  ownership metadata                                                                                          
                                                                                                                                                           
api_token
  hashed bearer tokens, each bound to one owner, one selected authorization
  context, a required expiration timestamp, and optionally narrowed by a group
  restriction

authorization_assignment
  roles and scopes such as edit/group, admin/global, or edit/self
```

Soft deletion should be used for deletes. Deleting an annotation marks it deleted, creates a new version and audit event, and removes it from normal active queries by default. Rows should not be physically removed as part of normal application behavior.

SAB should disallow inserts, direct updates, and accepted change sets that would create a duplicate active annotation once an agreed duplicate definition is in place. Existing source data may already contain duplicates, and making the full database duplicate-free is a longer-term cleanup goal. The initial write path should therefore focus on not making the problem worse. To support this, SAB should centralize duplicate detection in an application-level policy component that computes a canonical duplicate key or duplicate signature from the validated annotation payload. That key can be stored on the current annotation projection and indexed for lookup before writes. After existing duplicate data has been cleaned up, SAB can add stricter database-level protection, such as a partial unique index over active curated annotations.

## API and Application Layer

SAB should be implemented as a Python service using FastAPI, Pydantic, SQLAlchemy, and Alembic. FastAPI provides the HTTP application layer and OpenAPI generation. Pydantic models define request/response validation at the API boundary. SQLAlchemy and Alembic define the persistence model and migrations.

The public API should use conventional JSON over HTTP and be documented through OpenAPI. Resource-oriented endpoints should cover ordinary annotation operations:

```
GET    /annotations/{annotation_id}
  Read an existing annotation

POST   /annotations
  Create a new annotation

PATCH  /annotations/{annotation_id}
  Modify an existing annotation

DELETE /annotations/{annotation_id}
  Delete an existing annotation

GET    /annotations?subject=...&ontology_class_id=...&evidence_code=...
  List existing annotations with optional filters

GET    /annotations/{annotation_id}/versions
  List versions of an existing annotation

GET    /annotations/{annotation_id}/versions/{version}
  Read a specific version of an existing annotation

GET    /annotations/{annotation_id}/comments
  List comments for an existing annotation

POST   /annotations/{annotation_id}/comments
  Create a comment against the annotation's current version

PATCH  /annotations/{annotation_id}/comments/{comment_id}
  Edit an existing comment

DELETE /annotations/{annotation_id}/comments/{comment_id}
  Soft-delete an existing comment
```

Pagination should be used for "list" endpoints. The initial annotation list API should support structured filtering across the modeled Standard Annotation fields, except annotation extensions and annotation properties. Scalar fields should be stored directly in indexed PostgreSQL columns. Multivalued filterable fields can be projected into child tables so that filtering does not depend on JSONB scans.

Ontology-valued fields should support both direct filtering and closure-based filtering. A direct filter such as:

```
GET /annotations?ontology_class_id=GO:0003823
```

matches annotations that directly use `GO:0003823`. A closure filter should name the ontology property to close over:

```
GET /annotations?ontology_class_id=GO:0003823&ontology_class_id_closure=rdfs:subClassOf
```

This returns annotations whose `ontology_class_id` is `GO:0003823` (antigen binding) or a term in the precomputed `rdfs:subClassOf` closure beneath it, such as `GO:1990405` (protein antigen binding). Closure requests should only be accepted for ontology-valued fields and for predicates loaded by the ontology import job described below.

SAB should support two modification modes:

```
Direct CRUD (described above):
  Client writes directly to an annotation.
  SAB validates, checks expected version, writes the new current state,
  creates an immutable version, and records audit events.

Change sets (described in the following section):
  Client proposes an annotation creation, update, or deletion.
  SAB stores the proposed change, computes a preview, validates the result,
  and allows another client/user to accept or reject it.
```

SAB should not prefer one mode globally. Clients can choose the path that fits their workflow. Both modes must share the same underlying validation, versioning, soft-delete, and audit behavior.

### Annotation Comments

SAB should support a narrow annotation comment resource for general commentary about an annotation. Comments are application metadata, not part of the core Standard Annotation payload, and should not be included in the Standard Annotation LinkML schema. The initial design should not include topics, subtopics, or categories. Classification mechanisms could be added later if there is a clear need, but SAB should prefer proper data modeling and application support for structured concepts before relying on comment topics.

When a comment is created, SAB should attach it to the target annotation and record the annotation version that was current at creation time. The client should not choose this version; SAB should derive it from the current annotation row when the comment is written. This lets clients decide whether to show, hide, group, or flag comments that were made against older versions after an annotation changes.

Creating, editing, or deleting a comment should not create a new annotation version, affect duplicate identity, or alter GPAD/GPI export behavior. A comment may be edited or deleted by its author or by a user with the `admin` role for the annotation. Comment creation, editing, and soft deletion should be audited as application events.

## Change Sets

Change sets should support proposed annotation creation, update, and deletion. The common `change_set` resource should record the requested operation, review state, reason, preview metadata, and server-derived proposal metadata. Update operations should use JSON Patch as the canonical machine-readable patch format. Create and delete operations should use simpler operation-specific payloads.

Example client-submitted update payload:

```json
{
  "operation": "update",
  "annotation_id": "550e8400-e29b-41d4-a716-446655440000",
  "base_version": 3,
  "patch_format": "application/json-patch+json",
  "patch": [
    { "op": "replace", "path": "/evidence_code", "value": "ECO:0000269" }
  ],
  "reason": "Updated evidence mapping after review."
}
```

An update change set targets a specific `annotation_id` and `base_version`. Accepting it applies the patch atomically only if the annotation is still at that base version. Preview applies the patch to the base annotation snapshot, validates the resulting annotation, and shows the proposed post-acceptance state.

Example client-submitted create payload:

```json
{
  "operation": "create",
  "annotation": {
    "...": "complete Standard Annotation payload"
  },
  "reason": "New annotation from curator review."
}
```

A create change set should carry the full candidate Standard Annotation payload. It does not require an existing `annotation_id` or `base_version`. Preview validates the candidate annotation, checks duplicate policy, determines ownership from token context or submitted SAB metadata as usual, and shows the annotation that would be created. Accepting the change set creates the annotation, assigns the SAB annotation ID, creates version `1`, and records audit events.

Example client-submitted delete payload:

```json
{
  "operation": "delete",
  "annotation_id": "550e8400-e29b-41d4-a716-446655440000",
  "base_version": 3,
  "reason": "Annotation no longer supported."
}
```

A delete change set should target a specific `annotation_id` and `base_version`, but should not require a patch. Preview shows the current annotation and indicates that it would be soft-deleted. Accepting the change set applies the normal soft-delete behavior only if the annotation is still at the requested base version: mark the annotation deleted, create a new annotation version, and record audit events.

These client-submitted payloads are not complete persisted `change_set` records. When SAB stores a change set, it should add server-derived metadata such as who proposed the change and when it was proposed. The proposer should be derived from the request token, and the proposal timestamp should be assigned using the current server time.

SAB may reject JSON Patch operations whose meaning is unclear for the Standard Annotation data model. In particular, some multivalued fields, such as references or with/from values, are conceptually unordered even if they are represented as JSON arrays. A positional patch such as "remove the first reference" is harder to review and can misrepresent the client's intent, which is usually "remove this specific reference." As an intentionally conservative starting point, SAB should reject positional array operations for fields it treats as unordered. Clients that need to modify one of these fields can use whole-field replacement, providing the complete desired value for the field in a deterministic order.

### Concurrency

If an update or delete change set targets a version of an annotation that is no longer the current version, the change set becomes stale; SAB should not apply it to the newer, current version.

Direct CRUD writes should also use optimistic concurrency. A client should identify the version it intends to modify, and SAB should reject stale writes rather than silently overwriting a newer annotation version.

## Authentication, Authorization, and Audit

### API Authentication

The primary initial authentication use case is programmatic API access. API clients should authenticate with SAB-issued bearer tokens:

```
Authorization: Bearer <sab_api_token>
```

Developers should obtain an SAB API token before configuring their client application. A token should map to either a human user or a service account, and SAB should use that mapping for authorization and audit.

All read and write API access should require an authenticated token. Every action must resolve to an authenticated identity (a human user, service account, or trusted internal worker acting on behalf of a recorded job requester) for audit purposes.

GitHub OAuth should remain the primary authentication mechanism for human users, but in the initial architecture its most important purpose is token issuance and administration. A developer or administrator logs into a small SAB-owned token management interface with GitHub, creates a named token, and copies the generated token value from the creation confirmation screen. The OpenAPI-generated interactive documentation can help developers test authenticated API calls once they already have a token, but it should not be the primary token management interface. Token creation, revocation, and service-account administration should have a small purpose-built SAB interface or protected administrative API.

### Authorization Model

Authorization should use two axes: role and scope. The role describes what kind of action is allowed. The scope describes which annotations that role applies to. Some scopes are tied to a group. In SAB, a group is the owner of a set of annotations for authorization purposes; group identifiers are stored as SAB metadata, not as fields in the Standard Annotation payload itself.

The user, group, and authorization tables can be populated from SAB-specific entries that can be added to the existing GO `users.yaml` file in [GitHub](https://github.com/geneontology/go-site/blob/master/metadata/users.yaml). Each synchronization should record the source repository, commit SHA, and timestamp. Service accounts should use the same role/scope model in local SAB metadata.

The existing `users.yaml` format should evolve. SAB should not remain permanently coupled to Noctua-specific fields like `authorizations.noctua.go.allow-edit`. Instead, the file should add SAB-specific authorization entries so that Noctua authorization and SAB authorization are separate concerns while still using the same reviewed metadata file.

Initial roles are:

```
read
  May read annotations, versions, and allowed job/status information within
  the allowed scope.

edit
  May create annotations, update and soft-delete annotations, and propose
  change sets within the allowed scope.

admin
  May accept/reject change sets, run privileged asynchronous jobs,
  manage authorization configuration, and perform administrative actions
  within the allowed scope.
```

Initial scopes are:

```
self
  Applies only to annotations created by the user. This supports trainee users
  who should not modify all annotations owned by their group. A self-scoped
  authorization entry must name a group so SAB can assign ownership when the
  user creates annotations.

group
  Applies to annotations whose owning group matches the group named in the
  authorization entry. A token restricted to a group further narrows requests
  to that group.

global
  Applies regardless of annotation owning group. A global grant does not name a
  group. This supports users who can edit or administer annotations across
  groups.
```

Example `users.yaml` entries:

```
- accounts:
    github: curatorA
  authorizations:
    sab:
      - group: 'http://informatics.jax.org'
        role: edit
        scope: group
  nickname: 'Example MGI Curator'
  organization: MGI
  xref: 'GOC:exa'

- accounts:
    github: curatorB
  authorizations:
    sab:
      - group: 'http://informatics.jax.org'
        role: edit
        scope: group
      - group: 'http://geneontology.org'
        role: admin
        scope: group
  nickname: 'Example Multi-Group Curator'
  organization: GO
  xref: 'GOC:exb'

- accounts:
    github: adminA
  authorizations:
    sab:
      - role: admin
        scope: global
  nickname: 'Example SAB Superadmin'
  organization: GO Central
  xref: 'GOC:admina'

- accounts:
    github: curatorC
  authorizations:
    sab:
      - role: edit
        scope: global
      - group: 'http://informatics.jax.org'
        role: admin
        scope: group
  nickname: 'Example Global Editor / MGI Admin'
  organization: GO
  xref: 'GOC:exc'

- accounts:
    github: traineeA
  authorizations:
    sab:
      - group: 'http://www.ebi.ac.uk/GOA'
        role: edit
        scope: self
  nickname: 'Example Trainee Curator'
  organization: GOA
  xref: 'GOC:traineea'
```

The `go-site` quality-control checks for `users.yaml` should be extended to validate the SAB authorization shape. The SAB-specific checks should ensure that `role` is one of `read`, `edit`, or `admin`; `scope` is one of `self`, `group`, or `global`; `group` is required when `scope` is `self` or `group`; and `group` is omitted when `scope` is `global`. When present, `group` should have the expected group identifier shape. SAB should not require `authorizations.sab[].group` to match the legacy top-level `groups` field. SAB should treat `authorizations.sab[]` as the source of truth for SAB authorization.

### Bearer Token Context

SAB should generate high-entropy opaque tokens, display the raw token value only once at creation time, and store only a hash of the token. After creation, the raw token cannot be recovered; it can only be revoked and replaced. Every token should have a required expiration date. The token management interface should offer expiration options of one week, one month, one year, or a custom date no more than one year in the future; the backend should simply record the selected expiration date and reject expired tokens. Token metadata should include owner, optional restricted group, token type, created time, last-used time, expiration date, revocation status, selected authorization role/scope context, and audit metadata.

Each bearer token should be bound to an owner and to one selected SAB authorization entry. Token creation options should map directly to `authorizations.sab` entries. A `self` or `group` entry produces a group-restricted token option using the group named in that entry. A `global` entry produces an unrestricted token option.

A token's authorization context is used for two purposes: to define the scope of the token's privileges and to determine the default ownership for new annotations. A group-restricted token cannot read or write annotations outside its restricted group, and the restricted group also provides a default value for annotation ownership.

When SAB creates an annotation using a group-restricted token, the annotation's `owning_group_id` should be derived from the token context. If the client submits `owning_group_id` with a group-restricted token, it must match the token's restricted group. When SAB creates an annotation using an unrestricted token, the client must explicitly submit `owning_group_id` as SAB metadata outside the Standard Annotation payload, and SAB should allow this only when the token was created from an `edit/global` or `admin/global` authorization entry. SAB should also record the user or service account that created the annotation.

### Runtime Authorization

Runtime authorization decisions should use local database tables containing normalized users, groups, role/scope assignments, and token metadata. On each request, SAB should resolve the bearer token, load the token owner and optional restricted group, and then evaluate two things: the token's selected authorization context and the user's current SAB authorization grants. The token defines the maximum role/scope/group context for the request. The current authorization grants confirm that the user is still allowed to use that context. If the matching authorization entry has been removed from local database tables, SAB should reject the request even if the token has not been revoked yet.

If a group restriction is present on the token, the target annotation must belong to that group, and newly created annotations must use that group as their owning group. An unrestricted token imposes no additional group limit, so a token created from an `edit/global` or `admin/global` entry can use that grant across groups.

For example, the `curatorB` example above has two SAB authorization entries. The token management interface would offer two token options: an `edit/group` token for `http://informatics.jax.org` and an `admin/group` token for `http://geneontology.org`. A token created from the MGI edit entry can edit MGI-owned annotations and create MGI-owned annotations. It cannot administer GO Central annotations; that requires a separate token created from the GO Central admin entry.

### Audit

Audit should be first-class and append-only. The `audit_event` table should record important actions even when they do not create annotation versions: token creation/revocation, direct writes, soft deletes, change-set proposal/acceptance/rejection/staleness, imports, exports, authorization syncs, and administrative changes. Failed authentication and authorization attempts should be handled through application or security logs rather than stored as first-class database audit events in the initial design.

Annotation versions, change sets, and audit events should be linked but distinct:

```
annotation_version
  What did the annotation look like after an accepted change?

change_set
  What patch was proposed, reviewed, accepted, rejected, or made stale?

audit_event
  Who did what, when, through which workflow, and with what result?
```

## Import, Export, and Jobs

Imports, exports, ontology loads, and QC reports can be long-running, file-oriented, or scheduled operations, so SAB should run them as asynchronous jobs rather than ordinary request/response work. API requests should create durable job records and return `202 Accepted`; Celery workers should execute the jobs, Redis should serve as the broker, PostgreSQL should store durable job state, and S3-compatible object storage should hold uploaded inputs and generated artifacts where needed.

The common job API should look like:

```
POST /imports
  Creates an annotation import job and returns 202 Accepted with a job ID.

POST /exports
  Creates an annotation export job and returns 202 Accepted with a job ID.

POST /ontology-loads
  Creates a GO ontology load job and returns 202 Accepted with a job ID.
  The same job type may also be created by a scheduler for periodic loads.

POST /reports/annotation-qc
  Creates an annotation QC / semantic validation report job and returns
  202 Accepted with a job ID. The same job type may also be created by a
  scheduler for periodic reports.

GET /jobs/{job_id}
  Returns queued/running/succeeded/failed status, progress, counts,
  warnings/errors, and links to outputs where applicable.
```

Job handlers should be idempotent where practical and designed for retry without corrupting annotation state.

### Annotation Import and Export

Eventually, SAB's PostgreSQL database is the source of truth for Standard Annotations, and GPAD/GPI files can be exported from SAB when needed. In the transition period, GPAD/GPI files can be imported into the database as a means of bootstrapping it.

Until SAB becomes the source of truth for curated annotations, bulk GPAD/GPI import should use a drop-and-reload strategy: replace the current imported curated annotation set with records derived from the source files rather than attempting to merge imported records with existing SAB state. This is acceptable during the bootstrap period because SAB-side annotation changes are not treated as long-term authoritative until the chosen cut-over date. 

Import should parse source files, validate records against the Standard Annotation model, assign annotation IDs, store canonical annotation records, and record source provenance. During this drop-and-reload phase, import should not check imported annotations for duplicates because duplicates may already exist in current source files and identifying them is not the goal of the import. Duplicate detection for imported source data can instead be added through QC reporting. This import-specific behavior should not disable application-layer duplicate checks for direct CRUD operations or accepted create/update change sets. Structurally invalid records should not be stored, but they should not fail the whole import job; they should be rejected and reported at the individual annotation level.

Export should derive GPAD/GPI files from current active SAB records. When an export job starts, SAB should determine the latest version number for each annotation included in the export and then generate the output from those immutable rows in `annotation_version`. This gives the job a stable view of the data even if annotations are edited while the export is running. Export jobs should record enough metadata to make outputs explainable: initiating actor, timestamp, export parameters, schema/model version, code version if available, included annotation versions or a manifest that identifies them, counts, and warnings/errors.

### QC Reporting

Annotation QC reporting and semantic validation, including GORULE-style checks, should use the asynchronous job model. SAB may run selected blocking validation synchronously during writes, but full QC/report generation should be handled as manually triggered or periodic jobs so that large scans do not block normal API requests. Report jobs should record the rule set version, input selection, snapshot boundary or included annotation versions, counts, findings, warnings/errors, and links to report artifacts.

### Ontology Loading

SAB should load the GO ontology from the latest available `go-edit.obo` file in the [`geneontology/go-ontology`](https://github.com/geneontology/go-ontology) repository and record the source commit hash as the main source metadata. The target is to reload the ontology as frequently as is practical, ideally multiple times per week, though the exact cadence can remain flexible. 

A successful load should compute closure rows for an approved list of predicates such as `rdfs:subClassOf` and `BFO:0000050` (part of), store source metadata, and mark the new ontology version active only after the load succeeds. A closure row should record the subject term, predicate, object term, and depth, where subject is the more specific matched term and object is the broader queried term.

The ontology loading process should also scan the loaded ontology for term replacement metadata. When a current annotation uses a term with a single `replaced_by` value, SAB should update that annotation to use the replacement through the normal mutation path: create a new annotation version and record an audit event tied to the ontology load job. Cases where SAB cannot safely choose an automatic action, such as a term obsoleted without replacement, a term removed from the ontology, or a term with multiple possible alternatives through `consider`, should be reported through QC reporting jobs.

## Deployment and Operations

SAB should be containerized and designed to fit GO's existing AWS-oriented deployment practices without requiring complex DevOps practices.

The service stack should have a small number of moving parts:

```
FastAPI web container
  Handles HTTP API requests, authentication callbacks, OpenAPI docs, and job creation.

Celery worker container
  Runs import/export jobs and other long-running background tasks.

PostgreSQL
  Authoritative relational database, preferably AWS RDS in deployed environments.

Redis
  Self-managed Redis container used as the Celery broker.

S3-compatible object storage
  Stores uploaded import files, generated export files, and large job artifacts.
```

Local development should use Docker Compose to run the web app, worker, PostgreSQL, and Redis together. The local stack should be close enough to production that developers can test API behavior, migrations, jobs, and import/export workflows without needing AWS access.

For AWS deployment, the default recommendation is ECS/Fargate or an equivalent simple container service, RDS PostgreSQL, a self-managed Redis container, S3, and CloudWatch logs/metrics. Kubernetes, service meshes, bespoke event architectures, and complex observability platforms should be avoided unless future scale or organizational requirements clearly justify them.

PostgreSQL managed via AWS RDS is recommended here because of its built-in managed backup, point-in-time recovery, and monitoring features. We could potentially build this functionality into a self-managed container. However, since SAB will eventually hold source-of-truth data the risk of a backup failure is very high, and using a managed solution is a good tradeoff.

Redis, on the other hand, is only the message broker for Celery tasks. It should be treated as replaceable coordination infrastructure, not as the durable record of job state.

SAB should have at least two persistent deployed environments: `dev` and `prod`. The `dev` environment should mirror the production system and use separate databases, Redis instances, S3 buckets, OAuth credentials, secrets, and configuration. It should be used to preview merged code before production deployment. It should not share mutable production data except through explicit, controlled snapshots or imports.

The deployment flow should be simple and mostly automated. When a GitHub PR is merged, CI should build and publish the application image, run the required checks, apply migrations to the `dev` environment, and deploy the web and worker services there. Production deployment should be more controlled: a human operator should be able to promote a tested image to `prod` through a push-button workflow, with migrations and service rollout handled by the deployment pipeline rather than by manual shell access.

Database migrations should use Alembic and run as an explicit release step, separate from ordinary application startup. For example, a deployment can run a one-off ECS/Fargate task using the new application image and the command `alembic upgrade head`; only after that task succeeds should the web and worker services be updated to the new image. If the migration fails, the deployment should stop before new application containers are rolled out. Application startup should not silently run migrations in production.

Operational requirements should include:

* structured JSON logs  
* health/readiness endpoints  
* database backups and restore testing  
* basic CloudWatch alarms for service health, worker failures, queue depth, and database capacity  
* configuration through environment variables or a standard secret/config mechanism  
* separation of application secrets from source control  
* repeatable deployment documentation

The target should be boring and supportable: a small Python service stack that GO developers can run locally and operate in AWS with limited specialized DevOps burden.

## Open Questions for Discussion

* How should SAB define duplicate annotations? As a baseline, SAB can align with Protein2GO's duplicate-rejection behavior (requires input from Protein2GO developers).  
* Will SAB always import GPI files as the source of annotated entity metadata? If not, what are alternate sources? How should SAB handle an incoming annotation which references an annotated entity not represented in SAB’s known entities?  
* Which GORULEs should be enforced synchronously during writes? Which should be deferred to asynchronous QC/reporting jobs? Which are outside the scope of SAB?

## Possible Future Expansion

*These are explicitly not goals for the initial implementation, but they are items that have been discussed as possibilities for the future. They are listed here to ensure that decisions made about the initial design do not preclude them.*

Eventually, the full set of curated annotations in SAB should be duplicate-free. That requires first defining duplicate identity (see Open Questions for Discussion) and then identifying, reviewing, and resolving duplicates already present in imported source data. Until that cleanup is complete, SAB should still reject new writes that would create additional duplicates according to the agreed policy. The Standard Annotation LinkML schema and the duplicate-key policy can also support reports and cleanup tools that identify existing duplicate clusters.

The initial design is intentionally very conservative about how it handles change sets which target a version of an annotation that is not the current version. It will treat such change sets as stale and not allow them to be applied. A possible future enhancement is to allow some stale change sets to be "fast-forwarded" when the changes made since the original annotation version do not conflict with the proposed patch. Conceptually this would involve determining whether the result of applying the patch current annotation version is equivalent to the result of applying the patch to the original target annotation then applying a patch representing the difference between the current and target versions.

SAB could eventually store read-only inferred from electronic annotation (IEA) records to make annotation QC and reporting more complete. These records should be managed separately from curated annotations, likely through drop-and-reload imports tied to source metadata and import run IDs. They should not be exposed through the normal annotation API, should not be editable through CRUD or change-set operations, and should not become authoritative SAB records. Their purpose would be to provide additional context available only to reporting and QC jobs.

SAB could also support frontend application clients in addition to programmatic clients. That would require adding a browser-oriented authentication flow, likely GitHub OAuth followed by a secure, HTTP-only SAB session cookie, while keeping backend authorization in the same role/scope/group model used for bearer tokens. Request handling should resolve either a bearer token or a browser session into a shared authorization context containing the authenticated identity, selected role/scope/group context, and token or session metadata for audit. The frontend should be responsible for login UX, context selection, and displaying available actions, but SAB should continue to enforce all read/write permissions, ownership rules, and audit recording on the backend.  
