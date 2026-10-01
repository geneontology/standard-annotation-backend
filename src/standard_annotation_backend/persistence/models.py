"""Define the PostgreSQL tables used by the annotation backend."""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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


class ChangeSetOperation(StrEnum):
    """Operations that a proposal can request."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class ChangeSetState(StrEnum):
    """Review states persisted for a change set."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"


class Base(DeclarativeBase):
    """Base class shared by all application database records."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class SabUserRecord(Base):
    """Store a synchronized user identity without OAuth credentials."""

    __tablename__ = "sab_user"
    __table_args__ = (
        Index(
            "uq_sab_user_github_login", func.lower(text("github_login")), unique=True
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    github_login: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SabGroupRecord(Base):
    """Associate a local group identity with its external annotation ownership key."""

    __tablename__ = "sab_group"

    group_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    group_key: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuthorizationAssignmentRecord(Base):
    """Retain grants after removal so issued tokens cannot acquire a later regrant."""

    __tablename__ = "authorization_assignment"
    __table_args__ = (
        CheckConstraint("role IN ('read', 'edit', 'admin')", name="role_allowed"),
        CheckConstraint(
            "(scope IN ('self', 'group') AND group_id IS NOT NULL) OR (scope = 'global' AND group_id IS NULL)",
            name="scope_group_consistent",
        ),
        UniqueConstraint("assignment_id", "user_id"),
        Index(
            "uq_authorization_assignment_active_context",
            "user_id",
            "role",
            "scope",
            "group_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("is_active"),
        ),
        Index("ix_authorization_assignment_group_id", "group_id"),
    )

    assignment_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("sab_user.user_id", ondelete="RESTRICT"),
    )
    role: Mapped[str] = mapped_column(String(16))
    scope: Mapped[str] = mapped_column(String(16))
    group_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("sab_group.group_id", ondelete="RESTRICT"),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    owner: Mapped[SabUserRecord] = relationship()
    group: Mapped[SabGroupRecord | None] = relationship()


class ApiTokenRecord(Base):
    """Store a digest and the immutable authorization selected for a user token."""

    __tablename__ = "api_token"
    __table_args__ = (
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("expires_at > created_at", name="expiration_after_creation"),
        CheckConstraint(
            "selected_role IN ('read', 'edit', 'admin')", name="selected_role_allowed"
        ),
        CheckConstraint(
            "(selected_scope IN ('self', 'group') AND selected_group_id IS NOT NULL) OR (selected_scope = 'global' AND selected_group_id IS NULL)",
            name="selected_scope_group_consistent",
        ),
        ForeignKeyConstraint(
            ["assignment_id", "user_id"],
            [
                "authorization_assignment.assignment_id",
                "authorization_assignment.user_id",
            ],
            ondelete="RESTRICT",
            name="fk_api_token_assignment_owner",
        ),
        Index("ix_api_token_user_id", "user_id"),
        Index("ix_api_token_assignment_id", "assignment_id"),
    )

    token_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("sab_user.user_id", ondelete="RESTRICT"),
    )
    assignment_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    selected_role: Mapped[str] = mapped_column(String(16))
    selected_scope: Mapped[str] = mapped_column(String(16))
    selected_group_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner: Mapped[SabUserRecord] = relationship(viewonly=True)
    assignment: Mapped[AuthorizationAssignmentRecord] = relationship(viewonly=True)


class TokenManagementSessionRecord(Base):
    """Store a short-lived digest for a user's token-management session."""

    __tablename__ = "token_management_session"
    __table_args__ = (
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("expires_at > created_at", name="expiration_after_creation"),
        Index("ix_token_management_session_user_id", "user_id"),
    )

    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("sab_user.user_id", ondelete="CASCADE")
    )
    digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner: Mapped[SabUserRecord] = relationship()


class AuthorizationRefreshRecord(Base):
    """Store the source and counts of an applied authorization refresh.

    The most recent record describes the authorization state now in effect. A
    refresh whose document matches it is not applied again.
    """

    __tablename__ = "authorization_refresh"
    __table_args__ = (
        CheckConstraint(
            "source_checksum ~ '^[0-9a-f]{64}$'",
            name="source_checksum_format",
        ),
    )

    refresh_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    source_type: Mapped[str] = mapped_column(String(20))
    source_locator: Mapped[str] = mapped_column(Text)
    source_revision: Mapped[str | None] = mapped_column(Text)
    source_checksum: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    summary: Mapped[dict[str, object]] = mapped_column(JSONB)


class JobRecord(Base):
    """Store the state of one asynchronous operation."""

    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint(
            "job_type IN ('authorization_refresh', 'entity_refresh', 'entity_retirement', 'ontology_refresh')",
            name="type_allowed",
        ),
        CheckConstraint(
            "jsonb_typeof(parameters) = 'object'",
            name="parameters_object",
        ),
        CheckConstraint(
            "jsonb_typeof(progress) = 'object'",
            name="progress_object",
        ),
        CheckConstraint(
            "jsonb_typeof(warnings) = 'array' AND NOT "
            "jsonb_path_exists(warnings, '$[*] ? (@.type() != \"string\")')",
            name="warnings_string_array",
        ),
        CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'",
            name="result_object",
        ),
        CheckConstraint(
            "(status = 'queued' AND started_at IS NULL "
            "AND completed_at IS NULL AND result IS NULL AND error IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND result IS NULL AND error IS NULL) OR "
            "(status = 'succeeded' AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND result IS NOT NULL "
            "AND error IS NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL "
            "AND result IS NULL AND error IS NOT NULL "
            "AND error ~ '[^[:space:]]')",
            name="lifecycle_consistent",
        ),
        CheckConstraint(
            "updated_at >= created_at "
            "AND (started_at IS NULL OR started_at >= created_at) "
            "AND (completed_at IS NULL OR completed_at >= created_at) "
            "AND (started_at IS NULL OR completed_at IS NULL "
            "OR completed_at >= started_at)",
            name="timestamps_ordered",
        ),
        Index("ix_job_status_created_at_job_id", "status", "created_at", "job_id"),
    )

    job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    job_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50))
    requested_by: Mapped[str] = mapped_column(Text)
    parameters: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    progress: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    warnings: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EntityCatalogSnapshotRecord(Base):
    """Store one entity refresh's source details, counts, and publication state."""

    __tablename__ = "entity_catalog_snapshot"
    __table_args__ = (
        CheckConstraint(
            "source_checksum ~ '^[0-9a-f]{64}$'", name="source_checksum_format"
        ),
        CheckConstraint(
            "jsonb_typeof(source_metadata) = 'object'",
            name="source_metadata_object",
        ),
        CheckConstraint(
            "jsonb_typeof(source_statistics) = 'object'",
            name="source_statistics_object",
        ),
        CheckConstraint(
            "jsonb_typeof(record_statistics) = 'object'",
            name="record_statistics_object",
        ),
        CheckConstraint(
            "publication_result IS NULL OR jsonb_typeof(publication_result) = 'object'",
            name="publication_result_object",
        ),
        CheckConstraint(
            "source_key ~ '^[a-z0-9][a-z0-9_-]*$'", name="source_key_format"
        ),
        CheckConstraint(
            "(retired_at IS NULL) = (retired_by_job_id IS NULL) "
            "AND (retired_at IS NULL) = (retirement_result IS NULL)",
            name="retirement_complete",
        ),
        CheckConstraint("retired_at IS NULL OR NOT active", name="retired_inactive"),
        CheckConstraint(
            "retirement_result IS NULL OR jsonb_typeof(retirement_result) = 'object'",
            name="retirement_result_object",
        ),
        Index(
            "uq_entity_catalog_snapshot_active_source",
            "source_key",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("job.job_id", ondelete="RESTRICT"),
        unique=True,
    )
    source_key: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(20))
    source_locator: Mapped[str] = mapped_column(Text)
    source_revision: Mapped[str | None] = mapped_column(Text)
    source_checksum: Mapped[str] = mapped_column(String(64))
    source_format: Mapped[str] = mapped_column(String(20))
    source_metadata: Mapped[dict[str, object]] = mapped_column(JSONB)
    source_statistics: Mapped[dict[str, object]] = mapped_column(JSONB)
    record_statistics: Mapped[dict[str, object]] = mapped_column(JSONB)
    publication_result: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    staged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_job_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("job.job_id", ondelete="RESTRICT"),
        unique=True,
    )
    retirement_result: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True)
    )


class EntityStagingRecord(Base):
    """Store one validated entity record until its catalog is published."""

    __tablename__ = "entity_staging_record"
    __table_args__ = (
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("jsonb_typeof(entity) = 'object'", name="entity_object"),
        Index("ix_entity_staging_record_db_object_id", "db_object_id"),
    )

    job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("job.job_id", ondelete="CASCADE"),
        primary_key=True,
    )
    line_number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    db_object_id: Mapped[str] = mapped_column(Text)
    entity: Mapped[dict[str, object]] = mapped_column(JSONB)


class EntityMembershipRecord(Base):
    """Record that an entity identifier is active and which source supplies it.

    Annotations deliberately have no foreign key to this table. Replacing or
    retiring a catalog must be able to remove an identifier while existing
    annotations and their version history stay unchanged. The rule that a new or
    updated annotation's `db_object_id` must be active is enforced instead by
    `EntityRepository.require_active`, which checks the identifier and locks its
    row while the annotation is saved.
    """

    __tablename__ = "entity_membership"
    __table_args__ = (
        CheckConstraint(
            "source_key ~ '^[a-z0-9][a-z0-9_-]*$'", name="source_key_format"
        ),
        Index("ix_entity_membership_source_key", "source_key"),
    )

    db_object_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_key: Mapped[str] = mapped_column(Text)
    snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("entity_catalog_snapshot.snapshot_id", ondelete="RESTRICT"),
    )


class EntitySourceRecord(Base):
    """Store one source file record for an active entity identifier.

    An identifier may appear on several lines of a source file, so each record is
    stored separately with its line number. Records are deleted with their
    membership row when the catalog is replaced or retired.
    """

    __tablename__ = "entity_source_record"
    __table_args__ = (
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("jsonb_typeof(entity) = 'object'", name="entity_object"),
    )

    db_object_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("entity_membership.db_object_id", ondelete="CASCADE"),
        primary_key=True,
    )
    line_number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity: Mapped[dict[str, object]] = mapped_column(JSONB)


class OntologyMetadataRecord(Base):
    """Store source details and activation state for one ontology snapshot."""

    __tablename__ = "ontology_metadata"
    __table_args__ = (
        CheckConstraint(
            "source_checksum ~ '^[0-9a-f]{64}$'", name="source_checksum_format"
        ),
        CheckConstraint(
            "jsonb_typeof(loaded_predicates) = 'array' AND NOT "
            "jsonb_path_exists(loaded_predicates, "
            "'$[*] ? (@.type() != \"string\")')",
            name="loaded_predicates_string_array",
        ),
        CheckConstraint(
            "NOT active OR bulk_data_pruned_at IS NULL",
            name="active_bulk_data_complete",
        ),
        Index(
            "uq_ontology_metadata_active_key",
            "ontology_key",
            unique=True,
            postgresql_where=text("active"),
        ),
        Index(
            "ix_ontology_metadata_source",
            "ontology_key",
            "source_type",
            "source_locator",
            "source_revision",
            "source_checksum",
        ),
    )

    version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    staging_sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    ontology_key: Mapped[str] = mapped_column(String(100))
    source_type: Mapped[str] = mapped_column(String(100))
    source_locator: Mapped[str] = mapped_column(Text)
    source_revision: Mapped[str | None] = mapped_column(Text)
    source_checksum: Mapped[str] = mapped_column(String(64))
    document_version: Mapped[str | None] = mapped_column(Text)
    loaded_predicates: Mapped[list[str]] = mapped_column(JSONB)
    refresh_result: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("job.job_id", ondelete="RESTRICT"),
        unique=True,
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    bulk_data_pruned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class OntologyTermRecord(Base):
    """Store one term, its obsolete status, and its replacement suggestions."""

    __tablename__ = "ontology_term"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(replaced_by) = 'array' AND NOT "
            "jsonb_path_exists(replaced_by, '$[*] ? (@.type() != \"string\")')",
            name="replaced_by_string_array",
        ),
        CheckConstraint(
            "jsonb_typeof(consider) = 'array' AND NOT "
            "jsonb_path_exists(consider, '$[*] ? (@.type() != \"string\")')",
            name="consider_string_array",
        ),
    )

    version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("ontology_metadata.version_id", ondelete="CASCADE"),
        primary_key=True,
    )
    term_id: Mapped[str] = mapped_column(Text, primary_key=True)
    obsolete: Mapped[bool] = mapped_column(Boolean)
    replaced_by: Mapped[list[str]] = mapped_column(JSONB)
    consider: Mapped[list[str]] = mapped_column(JSONB)


class OntologyClosureRecord(Base):
    """Store the shortest path length between two terms for one predicate."""

    __tablename__ = "ontology_closure"
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id", "subject_term_id"],
            ["ontology_term.version_id", "ontology_term.term_id"],
            ondelete="CASCADE",
            name="fk_ontology_closure_subject",
        ),
        ForeignKeyConstraint(
            ["version_id", "object_term_id"],
            ["ontology_term.version_id", "ontology_term.term_id"],
            ondelete="CASCADE",
            name="fk_ontology_closure_object",
        ),
        CheckConstraint("depth >= 0", name="depth_nonnegative"),
        Index(
            "ix_ontology_closure_object_predicate",
            "version_id",
            "object_term_id",
            "predicate_id",
        ),
    )

    version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    subject_term_id: Mapped[str] = mapped_column(Text, primary_key=True)
    predicate_id: Mapped[str] = mapped_column(Text, primary_key=True)
    object_term_id: Mapped[str] = mapped_column(Text, primary_key=True)
    depth: Mapped[int] = mapped_column(Integer)


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
            "(record_origin = 'direct' AND source_import_job_id IS NULL)",
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
        CheckConstraint("body ~ '[^[:space:]]'", name="body_nonblank"),
        Index(
            "ix_annotation_comment_annotation_id_created_at_comment_id",
            "annotation_id",
            "created_at",
            "comment_id",
        ),
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


class ChangeSetRecord(Base):
    """Store a proposal and the outcome of its single terminal review."""

    __tablename__ = "change_set"
    __table_args__ = (
        CheckConstraint(
            "(operation = 'create' AND base_version IS NULL "
            "AND annotation_payload IS NOT NULL "
            "AND jsonb_typeof(annotation_payload) = 'object' AND patch IS NULL "
            "AND ((state = 'accepted' AND annotation_id IS NOT NULL) OR "
            "(state <> 'accepted' AND annotation_id IS NULL))) OR "
            "(operation = 'update' AND annotation_id IS NOT NULL "
            "AND base_version IS NOT NULL AND base_version > 0 "
            "AND annotation_payload IS NULL AND patch IS NOT NULL "
            "AND jsonb_typeof(patch) = 'array') OR "
            "(operation = 'delete' AND annotation_id IS NOT NULL "
            "AND base_version IS NOT NULL AND base_version > 0 "
            "AND annotation_payload IS NULL AND patch IS NULL)",
            name="operation_fields_consistent",
        ),
        CheckConstraint(
            "(state = 'proposed' AND reviewed_by IS NULL "
            "AND reviewed_at IS NULL AND review_reason IS NULL) OR "
            "(state IN ('accepted', 'rejected', 'stale') "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="state_review_consistent",
        ),
        CheckConstraint(
            "state <> 'rejected' OR "
            "(review_reason IS NOT NULL AND review_reason ~ '[^[:space:]]')",
            name="rejection_reason_nonblank",
        ),
        CheckConstraint(
            "(state = 'accepted' AND result_annotation_version IS NOT NULL "
            "AND result_annotation_version > 0) OR "
            "(state <> 'accepted' AND result_annotation_version IS NULL)",
            name="accepted_result_version_consistent",
        ),
        CheckConstraint(
            "(preview IS NULL AND previewed_at IS NULL) OR "
            "(preview IS NOT NULL AND jsonb_typeof(preview) = 'object' "
            "AND previewed_at IS NOT NULL)",
            name="preview_metadata_consistent",
        ),
        Index("ix_change_set_annotation_id", "annotation_id"),
        Index("ix_change_set_state", "state"),
        Index(
            "ix_change_set_proposed_at_change_set_id", "proposed_at", "change_set_id"
        ),
    )

    change_set_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    operation: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), server_default=text("'proposed'"))
    owning_group_id: Mapped[str] = mapped_column(Text)
    annotation_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("annotation.annotation_id", ondelete="CASCADE"),
    )
    base_version: Mapped[int | None] = mapped_column(Integer)
    annotation_payload: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    patch: Mapped[list[dict[str, object]] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    reason: Mapped[str] = mapped_column(Text)
    preview: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    previewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    proposed_by: Mapped[str] = mapped_column(Text)
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reviewed_by: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(Text)
    result_annotation_version: Mapped[int | None] = mapped_column(Integer)


class AuditEventRecord(Base):
    """Store durable context about an application operation."""

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_annotation_id", "annotation_id"),
        Index("ix_audit_event_job_id", "job_id"),
        Index("ix_audit_event_change_set_id", "change_set_id"),
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
