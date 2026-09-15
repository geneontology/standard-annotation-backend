"""Expose repositories that share a caller-managed transaction."""

from standard_annotation_backend.persistence.repositories.annotations import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
    AnnotationRepository,
    AnnotationSearchFilters,
    DuplicateAnnotationError,
    InvalidAnnotationProvenanceError,
    StaleAnnotationVersionError,
)
from standard_annotation_backend.persistence.repositories.audit import AuditRepository
from standard_annotation_backend.persistence.repositories.change_sets import (
    ChangeSetNotFoundError,
    ChangeSetRepository,
    InvalidChangeSetStateError,
)
from standard_annotation_backend.persistence.repositories.comments import (
    AnnotationCommentRepository,
    CommentNotFoundError,
    InvalidCommentError,
)
from standard_annotation_backend.persistence.repositories.pagination import Page

__all__ = [
    "AnnotationCommentRepository",
    "AnnotationDeletedError",
    "AnnotationNotFoundError",
    "AnnotationRepository",
    "AnnotationSearchFilters",
    "AuditRepository",
    "ChangeSetNotFoundError",
    "ChangeSetRepository",
    "CommentNotFoundError",
    "DuplicateAnnotationError",
    "InvalidAnnotationProvenanceError",
    "InvalidChangeSetStateError",
    "InvalidCommentError",
    "Page",
    "StaleAnnotationVersionError",
]
