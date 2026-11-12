"""List the identifiers in an annotation that semantic validation checks.

Each identifier comes with its location, written the way validation issues locate
values, so a check can report exactly which value is wrong.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.validation import ANNOTATION_ROOT


def split_identifier(identifier: str) -> tuple[str, str]:
    """Return an identifier's prefix and local identifier.

    The prefix is the text before the first `:`, and the local identifier is
    the rest, so `MGI:MGI:101757` splits into `MGI` and `MGI:101757`.
    """
    prefix, _, local_id = identifier.partition(":")
    return prefix, local_id


@dataclass(frozen=True, slots=True)
class IdentifierOccurrence:
    """Locate one identifier in an annotation.

    Attributes:
        identifier: The identifier, such as `GO:0008150`.
        location: Where it appears, such as `("annotation", "with_or_from", 1)`.
    """

    identifier: str
    location: tuple[str | int, ...]

    @property
    def prefix(self) -> str:
        """Return the text before the identifier's first `:`."""
        return split_identifier(self.identifier)[0]

    @property
    def local_id(self) -> str:
        """Return the text after the identifier's first `:`."""
        return split_identifier(self.identifier)[1]


def term_identifiers(annotation: Annotation) -> Iterator[IdentifierOccurrence]:
    """Yield every identifier that may name an ontology term.

    Identifiers are yielded in this order: `ontology_class_id`, each extension
    term, each `with_or_from` value, and each interacting taxon.

    Not every yielded identifier is an ontology term. `with_or_from` usually holds
    gene product or other sequence identifiers, such as `UniProtKB:P12345` or a
    ZFIN morpholino, and only sometimes a term, such as the GO term an IC
    annotation was inferred from. Callers must check each identifier's `prefix`
    and treat it as a term only when a configured ontology owns that prefix.
    """
    yield IdentifierOccurrence(
        annotation.ontology_class_id, (ANNOTATION_ROOT, "ontology_class_id")
    )
    for index, extension in enumerate(annotation.annotation_extensions or ()):
        yield IdentifierOccurrence(
            extension.extension_term,
            (ANNOTATION_ROOT, "annotation_extensions", index, "extension_term"),
        )
    for index, value in enumerate(annotation.with_or_from or ()):
        yield IdentifierOccurrence(value, (ANNOTATION_ROOT, "with_or_from", index))
    for index, taxon in enumerate(annotation.interacting_taxon_id or ()):
        yield IdentifierOccurrence(
            taxon, (ANNOTATION_ROOT, "interacting_taxon_id", index)
        )


def database_identifiers(annotation: Annotation) -> Iterator[IdentifierOccurrence]:
    """Yield the identifiers GORULE:0000027 checks against `db-xrefs.yaml`.

    These are `db_object_id`, each `with_or_from` value, and each extension
    term, in that order.
    """
    yield IdentifierOccurrence(
        annotation.db_object_id, (ANNOTATION_ROOT, "db_object_id")
    )
    for index, value in enumerate(annotation.with_or_from or ()):
        yield IdentifierOccurrence(value, (ANNOTATION_ROOT, "with_or_from", index))
    for index, extension in enumerate(annotation.annotation_extensions or ()):
        yield IdentifierOccurrence(
            extension.extension_term,
            (ANNOTATION_ROOT, "annotation_extensions", index, "extension_term"),
        )
