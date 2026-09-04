# New Standard Annotation Backend

This document proposes an architecture for a stable, initial version of the new Standard Annotation Backend (SAB). It is intended as a starting point for technical review and team discussion, not as a final implementation plan.

## Resources

* A human-readable definition of standard annotations can be found here: [https://geneontology.org/docs/go-annotations/\#standard-go-annotations](https://geneontology.org/docs/go-annotations/#standard-go-annotations)  
* A standard annotation is essentially one line from a GPAD file (along with the associated line from a GPI file). The format of these files is specified here: [https://github.com/geneontology/go-annotation/blob/master/specs/gpad-gpi-2-0.md](https://github.com/geneontology/go-annotation/blob/master/specs/gpad-gpi-2-0.md)  
* New SAB project issue: [https://github.com/geneontology/project-management/issues/130](https://github.com/geneontology/project-management/issues/130) (contains links to a few older brainstorming documents)

## Scope and Goals

SAB is a *de novo* replacement for GO's current standard annotation storage and access patterns. It is intended to be a long-term maintainable backend service that owns Standard Annotation records, exposes them through a stable programmatic API, supports reviewable change-set and direct mutation operations, and can derive GPAD/GPI-style outputs from its database.

After the bootstrap period and project-approved cutover, SAB is authoritative for Standard Annotation records and their lifecycle (creation, modification, deletion). Until that cutover, the imported GPAD/GPI sources remain authoritative and bootstrap replacement may discard SAB-side annotation records and their history as described below. SAB stores references to external identifiers, ontology terms, evidence terms, publications, and gene products, but it is explicitly not authoritative for those external resources. External metadata may be cached for display, search, validation, or export support, but those caches are not the source of truth.

Primary goals:

* Define a LinkML-based core Standard Annotation model.  
* Provide a conventional JSON-over-HTTP API documented with [OpenAPI](https://www.openapis.org/).  
* Store annotations in PostgreSQL using a relational-first model with JSONB snapshots.  
* Support both direct [CRUD](https://en.wikipedia.org/wiki/Create,_read,_update_and_delete#RESTful_APIs) writes and reviewable [JSON Patch](https://jsonpatch.com/)\-based change sets.  
* Preserve immutable annotation versions and first-class audit events.  
* Support API-triggered asynchronous GPAD/GPI import and export jobs.  
* Support semantic validation and reporting.  
* Prevent direct creates, direct updates, and accepted create/update change sets from creating duplicate annotations under a defined pairwise policy.
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

Existing annotations do not have stable IDs, so SAB must assign them. SAB annotation identifiers are backend-generated UUIDs stored as SAB metadata outside the Standard Annotation payload. They should not be added to the core Standard Annotation LinkML schema.

### Validation

Structural schema validation blocks all writes and all individual import records. SAB should synchronously enforce required fields, field types, cardinality, allowed enum-like values, and other LinkML/Pydantic constraints for direct CRUD and change-set acceptance. Structurally invalid imported records should be rejected and reported individually without failing the whole import. SAB should not store structurally invalid annotations; draft or quarantine storage for invalid annotations is not part of the initial design.

Initially, GORULE-style semantic validation should run asynchronously for reporting and should not block writes. Selection and versioning of the initial ruleset, as well as any future subset of rules that might block synchronously, remain deferred pending domain review. Semantic report infrastructure may proceed independently, but synchronous GORULE enforcement must wait for those decisions.

Except for checking bootstrap GPAD records against the matching GPI metadata supplied with the import, referential validation against external resources is not an initial design consideration. Other referential checks could be added later without major changes to the architecture being proposed now.

## Data Store and Versioning

SAB should use PostgreSQL as its authoritative data store. The schema should be relational-first. JSONB should be used judiciously for full annotation snapshots and flexible metadata, rather than as the only representation of an annotation.

The main `annotation` table should represent the current state of each annotation. Stable, commonly queried fields should be first-class columns with indexes: annotation ID, `db_object_id`, `ontology_class_id`, `evidence_type`, `assigned_by`, status, current version, created/updated timestamps, deletion state, record origin, and the source import job ID when applicable. More complex or repeated structures can use child tables where they are important for query or export behavior.

Each mutation should create an immutable annotation version. A lightweight `annotation_version` table should store the full LinkML-shaped annotation snapshot, version number, actor, timestamp, and the source of the change. The main `annotation` table stores the current version and query projection; historical versions are stored separately in `annotation_version`.

A proposed table sketch:

```
annotation
  current annotation state, ownership/group fields, indexed query fields, 
  soft-delete status, record origin/import provenance, indexed duplicate base
  signature; multivalued annotation fields needed for querying may be stored in
  child tables

annotation_duplicate_reference
  current-state projection with annotation ID, duplicate base signature, and
  one distinct canonical reference per row; indexed by base signature and
  canonical reference for conflict lookup

import_staging_annotation
  structurally valid annotations prepared by a bootstrap import job and keyed
  by job ID, isolated from live annotation state until publication

import_staging_annotation_duplicate_reference
  per-reference duplicate projections prepared for staged annotations and keyed
  by the same import job ID

annotation_version
  immutable full annotation snapshots, one row per accepted version

annotation_comment
  user-authored annotation comments with the annotation version observed at
  creation time, author, timestamps, body, and optional soft-delete metadata

change_set
  proposed create/update/delete annotation changes, review state, optional
  base annotation version, preview metadata

audit_event
  operational history across annotations, change sets, jobs, auth syncs, and
  admin actions; normally append-only, with a bootstrap replacement exception

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

Bootstrap imports should load into shared staging tables keyed by import job ID rather than directly into `annotation`. Staged rows are not visible to active queries or duplicate-conflict lookups. They enter the main annotation and projection tables only through the transactional publication process described below.

Soft deletion should be used for normal deletes. Deleting an annotation marks it deleted, creates a new version and audit event, and removes it from normal active queries by default. Rows should not be physically removed as part of normal application behavior; pre-cutover bootstrap replacement is the explicit exception.

### Duplicate Annotation Policy

The initial Protein2GO-aligned duplicate policy is a pairwise comparison over exactly these nine Standard Annotation schema slots:

* `db_object_id`
* `negation`
* `relation`
* `ontology_class_id`
* `evidence_type`
* `references`
* `with_or_from`
* `interacting_taxon_id`
* `annotation_extensions`

No other fields participate in duplicate comparison. In particular, dates, `assigned_by`, `annotation_properties`, and SAB ownership or audit metadata do not affect duplicate identity.

SAB should canonicalize the nine participating slots after structural schema validation as follows:

* A missing `negation` value is equivalent to `false`.
* A missing optional list is equivalent to an empty list.
* Identifiers compare exactly after structural/schema validation. SAB should not normalize CURIE aliases or treat ontology-equivalent identifiers as equal for this policy.
* Repeated identical elements in a list collapse to one element, and list order is ignored.
* `with_or_from` and `interacting_taxon_id` are sets and require complete set equality.
* `annotation_extensions` is a set of `(extension_relation, extension_term)` pairs and requires complete set equality.
* `references` is a set, but its comparison differs from the other lists: the reference portion matches when the two canonical reference sets have a non-empty intersection.

Two distinct annotations are duplicates exactly when both are active and non-deleted, all eight canonical non-reference components match, and their canonical reference sets overlap. This predicate is symmetric, but it is not necessarily transitive when schema-valid multi-reference annotations occur. For example, annotations with reference sets `{PMID:1}`, `{PMID:1, PMID:2}`, and `{PMID:2}` can form two overlapping duplicate pairs without the first and third annotations being duplicates. SAB must not take a transitive closure of these pairs, infer a global alias-equivalence relation between references, or introduce a conceptual-reference identity table. The schema should continue to support multi-reference annotations without imposing a cardinality of one. Currently, surveyed GPAD data did not contain multi-reference values, so this non-transitivity is an accepted initial risk that should be reviewed if multi-reference source data becomes material.

SAB should centralize this comparison in an application-level policy component. From each validated annotation payload, it should compute a deterministic base signature from the eight canonical non-reference components and store that signature on the indexed current annotation projection. It should also maintain indexed per-reference projection rows, one for each distinct canonical reference. A conflict lookup matches the submitted base signature and any submitted canonical reference against active, non-deleted annotations. The duplicate-conflict peer set for a candidate state is the set of annotation IDs returned by that lookup, excluding the candidate annotation's own ID.

When creating an annotation, SAB should reject the write if the candidate has any duplicate-conflict peer. For an update, SAB should compute the target's peer set before and after the proposed change, excluding the target's own annotation ID from both sets, and reject only when the after set contains at least one peer that was not in the before set. An update that changes only excluded fields, or otherwise preserves without expanding an imported legacy duplicate relationship, is allowed. A soft delete is allowed and removes the annotation's active duplicate conflicts. The same create, update, and delete semantics apply to accepted change sets.

This policy applies globally across active, non-deleted records, independent of source or ownership group. Existing source data may already contain duplicate pairs, and making the full database duplicate-free remains a longer-term cleanup goal. The signature and reference projections must be maintained with the current annotation state under the transaction and locking protocol described below. After legacy duplicate cleanup, SAB could add stricter database-level protection over active per-reference projections.

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

GET    /annotations?db_object_id=...&ontology_class_id=...&evidence_type=...
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
  SAB validates, applies the global create/update duplicate semantics and
  duplicate-state locking protocol, checks expected version, writes the new
  current state, creates an immutable version, and records audit events.

Change sets (described in the following section):
  Client proposes an annotation creation, update, or deletion.
  SAB stores the proposed change, computes a preview, validates the result,
  and allows another client/user to accept or reject it.
```

SAB should not prefer one mode globally. Clients can choose the path that fits their workflow. Both modes must share the same underlying validation, pairwise duplicate and locking policy, versioning, soft-delete, and audit behavior.

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
    { "op": "replace", "path": "/evidence_type", "value": "ECO:0000269" }
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

A create change set should carry the full candidate Standard Annotation payload. It does not require an existing `annotation_id` or `base_version`. Preview structurally validates the candidate annotation, determines ownership from token context or submitted SAB metadata as usual, applies the pairwise duplicate policy against current active, non-deleted records, and shows the annotation that would be created or the duplicate conflict that would prevent acceptance. Because database state may change after preview, acceptance must repeat the duplicate check after acquiring the required global shared annotation-write lock and signature lock. Accepting a non-duplicate change set creates the annotation, assigns the backend-generated SAB annotation ID, creates version `1`, and records audit events.

Example client-submitted delete payload:

```json
{
  "operation": "delete",
  "annotation_id": "550e8400-e29b-41d4-a716-446655440000",
  "base_version": 3,
  "reason": "Annotation no longer supported."
}
```

A delete change set should target a specific `annotation_id` and `base_version`, but should not require a patch. Preview shows the current annotation and indicates that it would be soft-deleted. Soft deletion is always allowed by the duplicate policy and removes the annotation's active duplicate conflicts. Accepting the change set applies the normal soft-delete behavior only if a fresh re-read under the required global shared annotation-write lock and signature lock shows that the annotation is still at the requested base version: mark the annotation deleted, update its duplicate projections, create a new annotation version, and record audit events.

These client-submitted payloads are not complete persisted `change_set` records. When SAB stores a change set, it should add server-derived metadata such as who proposed the change and when it was proposed. The proposer should be derived from the request token, and the proposal timestamp should be assigned using the current server time.

SAB may reject JSON Patch operations whose meaning is unclear for the Standard Annotation data model. In particular, some multivalued fields, such as references or with/from values, are conceptually unordered even if they are represented as JSON arrays. A positional patch such as "remove the first reference" is harder to review and can misrepresent the client's intent, which is usually "remove this specific reference." As an intentionally conservative starting point, SAB should reject positional array operations for fields it treats as unordered. Clients that need to modify one of these fields can use whole-field replacement, providing the complete desired value for the field in a deterministic order. Previewing an update change set should validate the patched annotation and compare the target's before and after duplicate-conflict peer sets, excluding the target itself from both. Acceptance must repeat that comparison against freshly read active, non-deleted records after acquiring the required global shared annotation-write lock and signature locks, and reject only if the proposed state adds a peer that was absent before.

### Concurrency

If an update or delete change set targets a version of an annotation that is no longer the current version, the change set becomes stale; SAB should not apply it to the newer, current version.

Direct CRUD writes should also use optimistic concurrency. A client should identify the version it intends to modify, and SAB should reject stale writes rather than silently overwriting a newer annotation version.

Duplicate checking is vulnerable to a concurrency race: two independent transactions can both find no conflicting annotation and then both write one. SAB must perform writes that involve the same duplicate base signature serially so only one transaction can check and write that state at a time, while allowing unrelated annotation writes to proceed concurrently.

Every annotation create, update, and soft delete, whether direct or performed by accepting a change set, should therefore run in a PostgreSQL `READ COMMITTED` transaction using transaction-scoped advisory locks. The transaction first takes the global annotation-write lock in shared mode, then takes exclusive locks for the affected old and new duplicate base signatures in a consistent order. Writes to dependent records such as comments and proposed change sets need only the shared global lock. Normal writes can share that global lock, while bootstrap publication takes it exclusively so those writes wait during publication.

After acquiring the locks, the transaction should freshly read the annotation version and duplicate conflicts before making the change. `READ COMMITTED` ensures that a transaction which waited for a lock sees the preceding transaction's commit when it performs those reads. The checks, annotation and version writes, duplicate-projection changes, and audit recording must then commit or roll back together; the advisory locks are released when the transaction ends.

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

Audit should be first-class and normally append-only. The `audit_event` table should record important actions even when they do not create annotation versions: token creation/revocation, direct writes, soft deletes, change-set proposal/acceptance/rejection/staleness, imports, exports, authorization syncs, and administrative changes. The only initial exception is pre-cutover bootstrap replacement, which permanently removes annotation-specific audit events associated with the superseded import while retaining the import job's aggregate audit events and report. Failed authentication and authorization attempts should be handled through application or security logs rather than stored as first-class database audit events in the initial design.

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

Before cutover, bulk GPAD/GPI import should replace the current imported annotation set in full rather than attempting to merge new source records with existing imported records. This is acceptable during the bootstrap period because SAB-side annotation changes are not treated as long-term authoritative until the chosen cutover date.

A bootstrap import must receive the GPAD data and matching GPI metadata as part of its input. An annotation whose annotated entity is absent from the imported GPI metadata should be rejected and reported at record level without failing the whole import. Alternative annotated-entity metadata sources and later reconciliation behavior are deferred.

Only one bootstrap import job may stage or publish at a time. Before staging, the worker should acquire a dedicated PostgreSQL session-level advisory lock for the bootstrap-import process and hold it until the job succeeds or fails; loss of the database session releases the lock. This prevents overlapping jobs from publishing source files out of order without blocking normal annotation reads or writes.

The bootstrap import job should parse the source files, structurally validate each record against the Standard Annotation model, assign backend-generated SAB UUIDs outside the Standard Annotation payload, and load the valid canonical records and their duplicate-signature/reference projections into staging tables keyed by the import job ID. It should also prepare each imported annotation's initial immutable version and source provenance. Structurally invalid records should be rejected and reported individually without failing the whole import. Staging is isolated from live annotation state and acquires neither the global annotation-write lock nor per-signature duplicate locks.

After staging succeeds, a publication transaction should acquire the global annotation-write lock in exclusive mode and permanently delete every annotation from the preceding bootstrap import together with its duplicate projections, immutable annotation versions, comments, targeted change sets, and annotation-specific audit events. The transaction should then insert the staged annotations, their initial immutable versions and duplicate projections, record their import-job provenance, and commit. The aggregate job record, job-level audit events, and import report remain as the durable record that the superseded import occurred. This destructive replacement is an explicit bootstrap-only exception to normal annotation soft deletion and history retention because SAB is not yet the authoritative source.

Publication does not acquire per-signature locks. PostgreSQL readers continue to see the previously committed annotation set while the transaction runs and see the complete replacement after it commits; they never see the intermediate delete-and-insert state. Every ordinary annotation mutation and write to an annotation-specific dependent record acquires the same global annotation-write lock in shared mode, so those writes wait during publication while reads remain available. If publication fails, the transaction rolls back and leaves the previous imported annotation set and all of its associated records intact. The consistent lock order is the global annotation-write lock first and signature locks second when signature locks are required; publication needs only the exclusive global annotation-write lock.

Bootstrap staging and publication perform no duplicate checks or duplicate rejection because duplicate pairs may already exist in current source files; the import job should not scan for duplicates even non-blockingly. This import exception does not apply to direct creates, direct updates, or accepted create/update change sets, which use the global pairwise duplicate policy.

Export should derive GPAD/GPI files from current active SAB records. When an export job starts, SAB should determine the latest version number for each annotation included in the export and then generate the output from those immutable rows in `annotation_version`. This gives the job a stable view of the data even if annotations are edited while the export is running. Export jobs should record enough metadata to make outputs explainable: initiating actor, timestamp, export parameters, schema/model version, code version if available, included annotation versions or a manifest that identifies them, counts, and warnings/errors.

### QC Reporting

Annotation QC reporting and GORULE-style semantic validation should use the asynchronous job model. Initially, these semantic checks are reporting-only and do not block writes. Selection and versioning of the initial ruleset, and the choice of any future rules that should block synchronously, remain deferred pending domain review. Semantic report infrastructure may proceed, but synchronous GORULE enforcement must wait for that decision. Full QC/report generation should be handled as manually triggered or periodic jobs so that large scans do not block normal API requests.

A separate QC process may apply the same pairwise duplicate predicate to bootstrap-imported data; the import job itself performs no duplicate checks or duplicate rejection. QC should report the annotation pairs that satisfy the predicate. Because reference overlap is not necessarily transitive for multi-reference annotations, QC must not claim that connected components of duplicate pairs are equivalence classes. Report jobs should record the applicable rule set or duplicate-policy version, input selection, source import job or included annotation versions, counts, findings, warnings/errors, and links to report artifacts.

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

## Decision Status and Deferred Inputs

| Decision area | Status | Owner or input needed | Implementation impact |
| --- | --- | --- | --- |
| SAB annotation identifiers | Settled for the initial implementation | No further input is needed. | The backend generates UUIDs as SAB metadata outside the Standard Annotation payload; UUID helpers and persistence may proceed. |
| Pre-cutover bulk GPAD/GPI import | Settled for the bootstrap period | Project leadership must eventually identify the cutover date, but no further input is needed for pre-cutover behavior. | A dedicated advisory lock permits only one bootstrap import job at a time. The job loads and validates staging tables keyed by job ID. One publication transaction then takes the global annotation-write lock exclusively, permanently deletes the preceding import and its annotation-specific dependent records, inserts the staged annotations, versions, and projections, and commits. Reads remain available; annotation writes wait during publication. |
| Duplicate handling during bootstrap import | Settled for the initial implementation | No further input is needed. | Staging and publication perform no duplicate checks or rejection. Staging takes no duplicate locks; publication takes the global annotation-write lock exclusively but takes no signature locks. A separate QC process may apply the settled pairwise predicate and should report duplicate pairs rather than treating connected components as equivalence classes. |
| Initial Protein2GO-aligned pairwise duplicate policy | Settled for the initial implementation | No further input is needed. The accepted non-transitivity risk should be reviewed if multi-reference source data becomes material. | Implementation may proceed with indexed base-signature and per-reference projections. Creates reject any peer; updates reject only newly introduced peers and may preserve legacy pairs; soft deletes remove conflicts. Every ordinary annotation mutation uses `READ COMMITTED`, takes the global annotation-write lock in shared mode before ordered signature locks, then freshly re-reads state and peers. |
| Annotated-entity metadata for bootstrap imports | Settled for bootstrap; alternatives are deferred | GO domain reviewers must identify any alternative metadata sources and desired later reconciliation behavior before either is implemented. | Bootstrap GPAD imports require matching GPI metadata as part of the input. An annotation whose entity is absent from that GPI metadata is rejected and reported at record level. No alternative source or reconciliation workflow is implied. |
| Structural validation | Settled for the initial implementation | No further policy input is needed. | Structural schema validation blocks all writes and individual import records. Invalid imported records are isolated and reported instead of failing the whole import. |
| GORULE-style semantic validation | Settled as initially asynchronous and non-blocking; exact ruleset and future blocking rules are deferred | GO domain reviewers must select and version the initial ruleset and decide whether any future rules should block synchronously. | Semantic reporting infrastructure may proceed. Initially GORULE-style checks do not block writes; synchronous enforcement must wait for the domain decision. |

## Possible Future Expansion

*These are explicitly not goals for the initial implementation, but they are items that have been discussed as possibilities for the future. They are listed here to ensure that decisions made about the initial design do not preclude them.*

Eventually, the full set of curated annotations in SAB should have no pairs that satisfy the settled duplicate predicate. Reaching that state requires identifying, reviewing, and resolving duplicate pairs already present in imported source data. During cleanup, creates with any duplicate-conflict peer are rejected, while direct updates and accepted update change sets are rejected only when they introduce a new peer; updates that preserve existing legacy pairs remain allowed, and soft deletes reduce those pairs. Reports and cleanup tools can use the base signature and per-reference projections, but any connected grouping they present for navigation must not be described as an equivalence class because the pairwise predicate is not necessarily transitive.

The initial design is intentionally very conservative about how it handles change sets which target a version of an annotation that is not the current version. It will treat such change sets as stale and not allow them to be applied. A possible future enhancement is to allow some stale change sets to be "fast-forwarded" when the changes made since the original annotation version do not conflict with the proposed patch. Conceptually this would involve determining whether the result of applying the patch current annotation version is equivalent to the result of applying the patch to the original target annotation then applying a patch representing the difference between the current and target versions.

SAB could eventually store read-only inferred from electronic annotation (IEA) records to make annotation QC and reporting more complete. These records should be managed separately from curated annotations, likely through drop-and-reload imports tied to source metadata and import run IDs. They should not be exposed through the normal annotation API, should not be editable through CRUD or change-set operations, and should not become authoritative SAB records. Their purpose would be to provide additional context available only to reporting and QC jobs.

SAB could also support frontend application clients in addition to programmatic clients. That would require adding a browser-oriented authentication flow, likely GitHub OAuth followed by a secure, HTTP-only SAB session cookie, while keeping backend authorization in the same role/scope/group model used for bearer tokens. Request handling should resolve either a bearer token or a browser session into a shared authorization context containing the authenticated identity, selected role/scope/group context, and token or session metadata for audit. The frontend should be responsible for login UX, context selection, and displaying available actions, but SAB should continue to enforce all read/write permissions, ownership rules, and audit recording on the backend.  
