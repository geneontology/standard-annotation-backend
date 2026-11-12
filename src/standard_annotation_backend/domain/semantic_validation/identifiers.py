"""List the identifiers in an annotation that semantic validation checks.

Each identifier comes with its location, written the way validation issues locate
values, so a check can report exactly which value is wrong.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.validation import ANNOTATION_ROOT


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
        return self.identifier.partition(":")[0]


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
