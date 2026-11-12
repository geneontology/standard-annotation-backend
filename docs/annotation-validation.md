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

The configured ontologies are listed in `config/sources.yaml`, each with the prefixes
it owns.

## When semantic validation applies

| Operation | Semantic validation |
|---|---|
| Create or update, directly or by accepting a change set | Any issue blocks the write |
| Change-set preview | The same checks; `can_accept` is false if any issue exists |
| Delete | Never checked |
| GPAD import | Never checked |

Every annotation version SABE writes, other than by import, passed semantic validation
when it was written. The checks use the reference data active when they run, so a later
refresh of an ontology, entity catalog, or the go-site metadata can give an existing
annotation issues. Imported annotations may have issues from the start. An annotation
with issues can be deleted, or updated to a state with no issues. That includes one of
two existing duplicates: it can be updated only once the other is deleted or changed.

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

Structural issue types are Pydantic's.

If a configured ontology or the go-site metadata has never been loaded, creates,
updates, change-set previews, and change-set acceptance return
`503 reference_data_unavailable`. A missing entity source is not an error; its
identifiers are reported as `unknown_entity`.
