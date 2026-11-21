"""Define the temporary tables a GPAD import stages its annotations in.

A GPAD refresh or cutover parses and validates its file into these tables, then
publication copies them into the live annotation tables, all in the job's one
transaction. Staged rows never need to outlive that transaction, so the tables are
PostgreSQL temporary tables created with `ON COMMIT DROP`: each import creates its
own, only its database session can see them, and PostgreSQL drops them when the
transaction commits or rolls back.

Being temporary also keeps the tables away from autovacuum, which never processes
temporary tables. Permanent staging tables were analyzed while empty between imports,
and the resulting statistics made each later import's foreign-key checks read every
row it had staged so far (#76). Temporary tables are also not written to the
write-ahead log, which saves I/O for data that is discarded.

These tables are not part of the migrated schema, so they use their own `MetaData`
rather than the declarative `Base`, and Alembic never compares or creates them.
Their columns match those of the live tables they are copied into. They have
primary keys and `CHECK` constraints but no foreign keys: the live tables' foreign
keys check the copied rows during publication.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Connection,
    Date,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID

STAGING_METADATA = MetaData()
"""Holds the staging tables, apart from the migrated schema's `Base.metadata`."""

STAGED_ANNOTATIONS = Table(
    "annotation_staging",
    STAGING_METADATA,
    Column("annotation_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("line_number", BigInteger, nullable=False),
    Column("annotation_data", JSONB, nullable=False),
    Column("duplicate_base_signature", String(64), nullable=False),
    Column("db_object_id", Text, nullable=False),
    Column("negation", Boolean, nullable=False),
    Column("relation", Text, nullable=False),
    Column("ontology_class_id", Text, nullable=False),
    Column("evidence_type", Text, nullable=False),
    Column("annotation_date", Date, nullable=False),
    Column("assigned_by", Text, nullable=False),
    CheckConstraint(
        "line_number > 0", name="ck_annotation_staging_line_number_positive"
    ),
    CheckConstraint(
        "jsonb_typeof(annotation_data) = 'object'",
        name="ck_annotation_staging_annotation_data_object",
    ),
    CheckConstraint(
        "duplicate_base_signature ~ '^[0-9A-Fa-f]{64}$'",
        name="ck_annotation_staging_duplicate_base_signature_format",
    ),
    prefixes=["TEMPORARY"],
    postgresql_on_commit="DROP",
)
"""One structurally valid annotation from the file, with its new SABE identifier."""

STAGED_MULTIVALUED_VALUES = Table(
    "annotation_staging_multivalued_value",
    STAGING_METADATA,
    Column("annotation_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("field_name", String(32), nullable=False),
    Column("field_value", Text, nullable=False),
    PrimaryKeyConstraint("annotation_id", "field_name", "field_value"),
    CheckConstraint(
        "field_name IN ('references', 'with_or_from', 'interacting_taxon_id')",
        name="ck_annotation_staging_multivalued_value_field_name_allowed",
    ),
    prefixes=["TEMPORARY"],
    postgresql_on_commit="DROP",
)
"""One value of a staged annotation's multivalued field, for filtering."""

STAGED_DUPLICATE_REFERENCES = Table(
    "annotation_staging_duplicate_reference",
    STAGING_METADATA,
    Column("annotation_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("canonical_reference", Text, nullable=False),
    Column("duplicate_base_signature", String(64), nullable=False),
    PrimaryKeyConstraint("annotation_id", "canonical_reference"),
    prefixes=["TEMPORARY"],
    postgresql_on_commit="DROP",
)
"""One canonical reference of a staged annotation, for finding duplicates."""


def create_staging_tables(connection: Connection) -> None:
    """Create the staging tables for one import on `connection`.

    The tables last until the connection's current transaction ends. Creation does
    not first check for existing tables, so a second import in one transaction
    fails at once instead of mixing two files' rows.
    """
    STAGING_METADATA.create_all(connection, checkfirst=False)
