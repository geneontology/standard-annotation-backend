"""Define the PostgreSQL tables used by the annotation backend."""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from standard_annotation_backend.domain.annotations import new_annotation_id

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class AnnotationStatus(StrEnum):
    """Values stored in an annotation's status column."""

    ACTIVE = "active"
    DELETED = "deleted"


class AnnotationOrigin(StrEnum):
    """Ways an annotation can enter the database."""

    DIRECT = "direct"
    IMPORT = "import"


class Base(DeclarativeBase):
    """Base class shared by all application database records."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class JobRecord(Base):
    """Define storage reserved for future background-job work."""

    __tablename__ = "job"

    job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    job_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50))
    requested_by: Mapped[str] = mapped_column(Text)
    parameters: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnnotationRecord(Base):
    """Store the current searchable form of an annotation."""

    __tablename__ = "annotation"
    __table_args__ = (
        CheckConstraint("current_version > 0", name="current_version_positive"),
        CheckConstraint(
            "(status = 'active' AND deleted_at IS NULL) OR "
            "(status = 'deleted' AND deleted_at IS NOT NULL)",
            name="status_deleted_at_consistent",
        ),
        CheckConstraint(
            "duplicate_base_signature ~ '^[0-9A-Fa-f]{64}$'",
            name="duplicate_base_signature_format",
        ),
        CheckConstraint(
            "(record_origin = 'import' AND source_import_job_id IS NOT NULL) OR "
            "(record_origin != 'import' AND source_import_job_id IS NULL)",
            name="record_origin_source_import_job_id_consistent",
        ),
        Index("ix_annotation_owning_group_id", "owning_group_id"),
        Index("ix_annotation_record_origin", "record_origin"),
        Index("ix_annotation_source_import_job_id", "source_import_job_id"),
        Index("ix_annotation_db_object_id", "db_object_id"),
        Index("ix_annotation_negation", "negation"),
        Index("ix_annotation_relation", "relation"),
        Index("ix_annotation_ontology_class_id", "ontology_class_id"),
        Index("ix_annotation_evidence_type", "evidence_type"),
        Index("ix_annotation_annotation_date", "annotation_date"),
        Index("ix_annotation_assigned_by", "assigned_by"),
    )

    annotation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_annotation_id
    )
    annotation_data: Mapped[dict[str, object]] = mapped_column(JSONB)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(
        String(16), default=AnnotationStatus.ACTIVE.value
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owning_group_id: Mapped[str] = mapped_column(Text)
    record_origin: Mapped[str] = mapped_column(String(32))
    source_import_job_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("job.job_id")
    )
    duplicate_base_signature: Mapped[str] = mapped_column(String(64))
    db_object_id: Mapped[str] = mapped_column(Text)
    negation: Mapped[bool] = mapped_column(Boolean)
    relation: Mapped[str] = mapped_column(Text)
    ontology_class_id: Mapped[str] = mapped_column(Text)
    evidence_type: Mapped[str] = mapped_column(Text)
    annotation_date: Mapped[date] = mapped_column(Date)
    assigned_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AnnotationVersionRecord(Base):
    """Store one unchanging version of an annotation."""

    __tablename__ = "annotation_version"
    __table_args__ = (CheckConstraint("version > 0", name="version_positive"),)

    annotation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("annotation.annotation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    annotation_data: Mapped[dict[str, object]] = mapped_column(JSONB)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    actor_id: Mapped[str] = mapped_column(Text)
    change_source: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AnnotationMultivaluedFieldValueRecord(Base):
    """Store one value from a multivalued field for database filtering."""

    __tablename__ = "annotation_multivalued_field_value"
    __table_args__ = (
        CheckConstraint(
            "field_name IN ('references', 'with_or_from', 'interacting_taxon_id')",
            name="field_name_allowed",
        ),
        Index(
            "ix_annotation_multivalued_field_value_lookup",
            "field_name",
            "field_value",
            "annotation_id",
        ),
    )

    annotation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("annotation.annotation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    field_name: Mapped[str] = mapped_column(String(32), primary_key=True)
    field_value: Mapped[str] = mapped_column(Text, primary_key=True)


class AnnotationDuplicateReferenceRecord(Base):
    """Store one reference used to find possible duplicate annotations."""

    __tablename__ = "annotation_duplicate_reference"
    __table_args__ = (
        CheckConstraint(
            "duplicate_base_signature ~ '^[0-9A-Fa-f]{64}$'",
            name="duplicate_base_signature_format",
        ),
        Index(
            "ix_annotation_duplicate_reference_conflict_lookup",
            "duplicate_base_signature",
            "canonical_reference",
            "annotation_id",
            unique=False,
        ),
    )

    annotation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("annotation.annotation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    canonical_reference: Mapped[str] = mapped_column(Text, primary_key=True)
    duplicate_base_signature: Mapped[str] = mapped_column(String(64))


class AnnotationCommentRecord(Base):
    """Store a comment attached to the annotation version it describes."""

    __tablename__ = "annotation_comment"
    __table_args__ = (
        ForeignKeyConstraint(
            ["annotation_id", "annotation_version"],
            ["annotation_version.annotation_id", "annotation_version.version"],
            name="fk_annotation_comment_annotation_version",
            ondelete="CASCADE",
        ),
        CheckConstraint("btrim(body) <> ''", name="body_nonblank"),
    )

    comment_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    annotation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    annotation_version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEventRecord(Base):
    """Define storage reserved for future audit events."""

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_annotation_id", "annotation_id"),
        Index("ix_audit_event_job_id", "job_id"),
    )

    audit_event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    action: Mapped[str] = mapped_column(String(100))
    actor_id: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(String(50))
    token_id: Mapped[str | None] = mapped_column(Text)
    token_name: Mapped[str | None] = mapped_column(Text)
    selected_role: Mapped[str | None] = mapped_column(String(100))
    selected_scope: Mapped[str | None] = mapped_column(String(100))
    selected_group_id: Mapped[str | None] = mapped_column(Text)
    annotation_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    annotation_version: Mapped[int | None] = mapped_column(Integer)
    comment_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    job_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    change_set_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    details: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
