"""Define errors for annotation comments."""

from uuid import UUID

from standard_annotation_backend.domain.errors import InvalidInputError, NotFoundError


class CommentNotFoundError(NotFoundError):
    """Report a comment that is missing, deleted, or under another annotation."""

    code = "comment_not_found"
    message = "Comment was not found"

    def __init__(self, comment_id: UUID) -> None:
        self.comment_id = comment_id
        super().__init__()


class InvalidCommentError(InvalidInputError):
    """Report a comment body with no visible text."""

    code = "invalid_comment"
    message = "Comment body must contain non-whitespace text"

    def issue_location(self) -> tuple[str | int, ...]:
        """Return the comment body field."""
        return ("body", "body")
