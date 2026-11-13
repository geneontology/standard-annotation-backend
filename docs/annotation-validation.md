# Annotation validation

This guide is for SABE clients and administrators. It describes how SABE decides
whether an annotation can be written and what it reports when it cannot.

## Two stages

SABE checks every annotation it would write in two stages.

1. **Structural validation** checks the payload against the Standard Annotation schema:
   required fields, types, identifier formats, and the relation and evidence value
   sets. A payload that fails it gets only structural issues, because the semantic
   checks cannot run reliably on it.
2. **Semantic validation** checks a structurally valid annotation against SABE's
   reference data and its other annotations:
   - **Entity:** `db_object_id` is an active entity in an entity catalog.
   - **Ontology terms:** every identifier whose prefix belongs to a configured
     ontology is a current term of it. It exists, is not obsolete, and is not an
     alternative identifier. The fields checked are `ontology_class_id`, each
     extension term, `with_or_from`, and `interacting_taxon_id`. Identifiers with
     other prefixes are not checked against any catalog.
   - **Duplicates:** no other active annotation is a duplicate under SABE's
     duplicate policy (see `ARCHITECTURE.md`).
   - **GO Rules:** the annotation satisfies every write-blocking GO Rule listed below.

The configured ontologies are listed in `config/sources.yaml`, each with the prefixes
it owns.

## When semantic validation applies

| Operation | Semantic validation |
|---|---|
| Create or update, directly or by accepting a change set | Any issue blocks the write |
| Change-set preview | The same checks; `can_accept` is false if any issue exists |
| Delete | Never checked |
| GPAD import | Never checked |
| Automatic replacement of obsolete terms | Same as an update; the annotation is skipped if the result would have issues |

Every annotation version SABE writes, other than by import, passed semantic validation
when it was written. The checks use the reference data active when they run, so a later
refresh of an ontology, entity catalog, or the go-site metadata can give an existing
annotation issues. Imported annotations may have issues from the start. An annotation
with issues can be deleted, or updated to a state with no issues. That includes one of
two existing duplicates: it can be updated only once the other is deleted or changed.

## GO Rules

SABE reads the GO Rules as follows:

- **Rules only restrict.** An annotation must pass every rule, and no rule grants an
  exception to another.
- **A rule that needs a missing fact does not fire.** The missing fact is already its
  own issue. An unknown or obsolete term gets no closure-based rules, and an unknown
  entity gets no rules that need its taxon or type. Fixing one issue can reveal others
  on the next attempt.
- **Rules about the annotated term apply only when it is a current term.**
- **Evidence is named by GO code.** A rule that names a GO evidence code (IPI, ISS, …)
  matches the ECO term whose schema `EvidenceType` alias is that code.
- **"Under X" means X or any of its `rdfs:subClassOf` descendants in GO. "Directly"
  means exactly X.**
- **An identifier's prefix** is the text before its first `:`, and its local identifier
  is the rest. For example, `MGI:MGI:101757` has prefix `MGI` and local identifier
  `MGI:101757`.
- **The three GO roots** are `GO:0003674` (MF), `GO:0008150` (BP), and `GO:0005575` (CC).

| Rule | An issue when |
|---|---|
| 02 | `negation` is true and the term is directly `GO:0005488` or `GO:0005515`. |
| 05 | The term is directly `GO:0005488` or `GO:0005515` and the evidence is IEA, ISS, ISO, ISM, ISA, or IBA. This follows the rule body; the title also lists RCA, and that question is raised upstream. |
| 06 | The evidence is IEP or HEP and the term is not under the BP root. |
| 07 | The evidence is IPI and the term is under `GO:0003824`. |
| 08 | The term is in subset `gocheck_do_not_annotate`. |
| 11 | The evidence is ND and the term is not a root, or the term is a root and the evidence is not ND. |
| 15 | `interacting_taxon_id` is non-empty and the term is not under `GO:0044419`, `GO:0043903`, or `GO:0018995`; or an interacting taxon equals the entity's own `db_object_taxon_id` (from its GPI record). |
| 16 | The evidence is IC and `with_or_from` has no `GO:` identifier other than the annotated term. |
| 17 | The evidence is IDA and `with_or_from` is non-empty. |
| 18 | The evidence is IPI and `with_or_from` is empty. |
| 22 | A reference is a PMID listed in `retracted-publications.txt`. |
| 27 | One of these fails: (a) every prefix in `db_object_id`, `with_or_from`, and extension terms must exactly match (case-sensitive) a `database` in `db-xrefs.yaml`, and synonyms are not accepted; (b) if that database defines any `id_syntax`, the local identifier must fully match at least one of them; (c) `assigned_by` must be a `shorthand` in `groups.yaml`. |
| 30 | A `GO_REF:` reference is absent from `gorefs.yaml`, is marked obsolete, or is an alt ID there. |
| 33 | A reference's prefix is not `PMID`, `DOI`, or `GO_REF`. SABE rejects such references; it does not repair them. |
| 37 | The evidence is IBA and `assigned_by` is not `GO_Central`, or the references are not exactly `{GO_REF:0000033}`. |
| 39 | The `db_object_id` prefix is `ComplexPortal` and the term is under `GO:0032991`. |
| 42 | The evidence is IKR and `negation` is not true. |
| 43 | The `eco-usage-constraints.yaml` entry for the evidence term is violated. See below. |
| 46 | The term is under `GO:0042803`, `GO:0051260`, `GO:0051289`, `GO:0070207`, `GO:0043621`, or `GO:0032840`, and `with_or_from` is non-empty but does not contain `db_object_id`. |
| 50 | The evidence is ISS, ISA, or ISO and `with_or_from` contains `db_object_id`. |
| 53 | `negation` is true and the annotation has any extension. |
| 55 | Two references have the same prefix. |
| 58 | An extension does not conform to `extensions-constraints.yaml`. See below. |
| 61 | The relation is not allowed for the term's branch. See below. |
| 63 | The evidence is ISS, ISA, or ISO and `with_or_from` is empty. |
| 65 | The term is in subset `gocheck_obsoletion_candidate`. |

**GORULE:0000043.** The rule uses the `eco-usage-constraints.yaml` entry whose
`eco_id` equals the annotation's evidence term. Entries for non-default ECO terms can
never match, because the schema allows only the defaults.

- `allowed_reference`: if set, at least one reference must match the pattern in full.
  Requiring every reference to match would reject a PMID cited with its DOI.
- `with_presence`:
  - `mandatory`: `with_or_from` must be non-empty.
  - `disallowed`: `with_or_from` must be empty.
  - `optional` and `recommended`: no check.
  - `conditional`: evaluate `with_presence_condition`, then apply `when_true` or
    `when_false` as above.
    - `db_object_type eq 'complex'` is true when the entity's type is under
      `GO:0032991`. If the entity is unknown, the condition cannot be evaluated and the
      check does not fire.
    - `reference matches '<pattern>'` is true when any reference matches the pattern.
- `with_entities` and `with_structure` are not enforced. They need entity types for
  `with_or_from` identifiers and the structure of `with_or_from`, and SABE has neither.

**GORULE:0000058.** This uses the `extensions-constraints.yaml` entries that name their
relation by RO or BFO ID (`relation_id`). The file may list several entries for the same
relation. An extension conforms when at least one entry for its relation meets all of
these:

1. It lists the prefix of the extension term among its `namespaces`.
2. The annotated term is under one of its `primary_root_terms`.
3. It has no `cardinality`, or the number of the annotation's extensions with that
   relation and the same extension term prefix is no more than the `cardinality`. This
   follows the file's header (go-site PR #2776), which limits "an extension with this
   relation and filler namespace" within one conjunction. An annotation's extensions
   are one conjunction.

An extension whose relation has no entry does not conform. Each extension that does
not conform is a separate issue, located at that extension.

**GORULE:0000061.** The allowed relations depend on the term's branch:

| Term | Allowed relations |
|---|---|
| Directly the MF root | `RO:0002327` |
| Under the MF root | `RO:0002327`, `RO:0002326` |
| Directly the BP root | `RO:0002331` |
| Under the BP root | `RO:0002331`, `RO:0002264`, `RO:0004032`, `RO:0004033`, `RO:0002263`, `RO:0004034`, `RO:0004035` |
| Directly the CC root | `RO:0002432` |
| Under `GO:0032991` | `BFO:0000050` |
| Under `GO:0110165` or `GO:0044423` | `RO:0001025`, `RO:0002432`, `RO:0002325` |

The most specific matching row applies, so a root term uses its "directly" row. If a
term falls under more than one of the last two rows, the allowed relations are the
union of those rows. A CC term that falls under neither of the last two rows has no
allowed relation. `negation` has no effect on this rule.

go-site patterns (`id_syntax`, `allowed_reference`, and `reference matches`) are matched
against whole identifiers. GORULE:0000055 counts distinct references, so the same
reference listed twice is allowed.

Rule 13 is deferred, rules 04, 49, and 54 are for QC reporting only, and rules that do
not apply to Standard Annotations, such as 09, 19, and 64, are not checked.

## Automatic term replacement

After every ontology activation, and after every GPAD publication for that group,
SABE runs a `term_replacement` job. It replaces each identifier that is obsolete with
exactly one `replaced_by`, or that is an alternative ID, with the current term at the
end of its chain. It follows chains within the identifier's ontology, in every field
the term checks cover. A chain that ends at a term that is not current, loops, or
leaves the ontology is not followed. GO_REF alternative IDs are not replaced. If a
replacement repeats a value already in the same list, such as two `with_or_from`
entries that lead to one term, only the first copy is kept.

Each replacement is written like a client update and must pass semantic validation.
An annotation whose replaced result would have any issue, including becoming a
duplicate, is left unchanged and keeps its issues. Replacement versions record the
change source `term_replacement` and the requester of the job that triggered the
replacement. They do not count as local changes when SABE decides whether a GPAD file
must be imported again.

## Reports

An annotation QC report validates every active annotation in its scope, all groups or
one owning group, with the same checks as client writes, and keeps one finding for each
annotation with at least one issue. Each finding lists every issue in the format write
responses use. A stored annotation that no longer passes the schema becomes a finding
whose issues have the issue type `structural`. Each annotation of a duplicate pair
lists its peers; reports do not group duplicates further.

- **Starting.** `POST /reports/annotation-qc` with an optional `owning_group_id`
  returns `202` and the job. It requires the admin role: a global admin reports on one
  group or on all groups, and a group admin on its own group. A queued report of the
  same scope is reused. An all-groups report also runs nightly, on
  `SABE_QC_REPORT_CRON` (default `0 6 * * *` UTC).
- **Reading.** `GET /reports/annotation-qc` lists reports newest first, optionally by
  `status`, and says whether each report's findings can still be read. A global reader
  sees every report, and other readers see all-groups reports and their own group's.
  The list only narrows discovery: any report can be read by ID, with counts and
  findings limited as described next.
  `GET /reports/annotation-qc/{report_id}` returns the status, scope, timing, the
  reference data versions used, and counts by issue type and by group.
  `GET /reports/annotation-qc/{report_id}/findings` lists findings, filtered by
  `issue_type` and `owning_group_id`. Reading requires the read role. Counts cover the
  reader's group, or every group for global scope, and findings follow the reader's
  ownership scope, as in annotation search.
- **Retention.** Summaries and counts are kept permanently. Findings are kept only for
  the most recent completed report of each scope, so an older report's findings
  return `410 report_findings_expired`. The findings of a report that has not
  succeeded return `409 report_not_completed`.
- **Missing reference data.** If a configured ontology or the go-site metadata has no
  active snapshot, the report fails with `reference_data_unavailable`.

## Errors

A create or update that fails returns `422 invalid_annotation`; accepting a change set
returns `422 invalid_change_set`. The response lists every issue found by the stage that
ran. Each issue has a `location`, a `type`, a `message`, and sometimes `details`.

| Type | Location | Details |
|---|---|---|
| `unknown_entity` | `("annotation", "db_object_id")` | — |
| `unknown_term` | the identifier | — |
| `obsolete_term` | the identifier | `replaced_by`, `consider` |
| `alternate_term_id` | the identifier | `primary_id` |
| `duplicate_annotation` | `("annotation",)` | `peer_ids` |
| `gorule_NNNNNNN` (the seven-digit rule number) | the identifier the rule is about, or `("annotation",)` | only GORULE:0000030 for an alternative GO_REF (`primary_id`) and GORULE:0000061 (`allowed_relations`) |

Issues are listed in a stable order: the entity check; the term checks for
`ontology_class_id`, extension terms, `with_or_from`, and interacting taxa, in that
order; the duplicate check; then GO Rules by number.

Structural issue types are Pydantic's.

If a configured ontology or the go-site metadata has never been loaded, creates,
updates, change-set previews, and change-set acceptance return
`503 reference_data_unavailable`. A missing entity source is not an error; its
identifiers are reported as `unknown_entity`.
