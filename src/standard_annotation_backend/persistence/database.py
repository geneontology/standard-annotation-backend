"""Create SQLAlchemy engines and sessions for the application database."""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def create_database_engine(database_url: str) -> Engine:
    """Create an engine for synchronous PostgreSQL access.

    Args:
        database_url: SQLAlchemy URL for the database.

    Returns:
        An engine that checks pooled connections before use and runs transactions
        with PostgreSQL's `READ COMMITTED` isolation level.
    """
    return create_engine(
        database_url,
        isolation_level="READ COMMITTED",
        pool_pre_ping=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the application's SQLAlchemy session factory.

    Args:
        engine: Engine that each session will use.

    Returns:
        A factory for sessions that flush explicitly and keep loaded values after
        a commit.
    """
    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )
