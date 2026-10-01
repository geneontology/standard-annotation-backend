"""Define the stable vocabulary stored in application audit events."""

from enum import StrEnum


class AuditAction(StrEnum):
    """Stable names for operations recorded in the audit trail."""

    ANNOTATION_CREATED = "annotation.created"
    ANNOTATION_UPDATED = "annotation.updated"
    ANNOTATION_DELETED = "annotation.deleted"
    ANNOTATION_COMMENT_CREATED = "annotation_comment.created"
    ANNOTATION_COMMENT_UPDATED = "annotation_comment.updated"
    ANNOTATION_COMMENT_DELETED = "annotation_comment.deleted"
    TOKEN_CREATED = "token.created"
    TOKEN_REVOKED = "token.revoked"
    AUTHORIZATION_SYNCHRONIZED = "authorization.synchronized"
    JOB_QUEUED = "job.queued"
    JOB_STARTED = "job.started"
    JOB_SUCCEEDED = "job.succeeded"
    JOB_FAILED = "job.failed"
    CHANGE_SET_PROPOSED = "change_set.proposed"
    CHANGE_SET_ACCEPTED = "change_set.accepted"
    CHANGE_SET_REJECTED = "change_set.rejected"
    CHANGE_SET_MARKED_STALE = "change_set.marked_stale"
    IMPORT_COMPLETED = "import.completed"
    EXPORT_COMPLETED = "export.completed"
    ONTOLOGY_LOADED = "ontology.loaded"
    ENTITY_CATALOG_PUBLISHED = "entity.catalog_published"
    ENTITY_CATALOG_RETIRED = "entity.catalog_retired"
    ADMINISTRATIVE_CHANGE = "administrative.change"


class AuditResult(StrEnum):
    """Outcomes stored on audit events."""

    SUCCESS = "success"
    FAILURE = "failure"
