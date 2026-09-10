"""Group repository changes into one database transaction."""

from collections.abc import Callable
from types import TracebackType

from sqlalchemy.orm import Session

from standard_annotation_backend.persistence.repositories import (
    AnnotationCommentRepository,
    AnnotationRepository,
)

SessionFactory = Callable[[], Session]
UnitOfWorkFactory = Callable[[], "SqlAlchemyUnitOfWork"]


class SqlAlchemyUnitOfWork:
    """Manage one session shared by the annotation and comment repositories.

    Use this class as a context manager. Call `commit()` to keep the changes;
    leaving the context without a commit rolls them back.

    Args:
        session_factory: Factory used to open the session.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self._committed = False
        self.annotations: AnnotationRepository
        self.comments: AnnotationCommentRepository

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        self._session = self._session_factory()
        self._committed = False
        self.annotations = AnnotationRepository(self._session)
        self.comments = AnnotationCommentRepository(self._session)
        return self

    def commit(self) -> None:
        """Commit the current transaction.

        Raises:
            RuntimeError: If the unit of work is not inside a context block.
        """
        session = self._require_session()
        session.commit()
        self._committed = True

    def rollback(self) -> None:
        """Roll back the current transaction.

        Raises:
            RuntimeError: If the unit of work is not inside a context block.
        """
        session = self._require_session()
        session.rollback()
        self._committed = False

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        try:
            if not self._committed:
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session


def create_unit_of_work_factory(
    session_factory: SessionFactory,
) -> UnitOfWorkFactory:
    """Create a factory for application units of work.

    Args:
        session_factory: Factory used to open a session for each unit of work.

    Returns:
        A zero-argument callable that creates a new unit of work.
    """

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    return factory
