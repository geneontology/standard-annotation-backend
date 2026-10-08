"""Define annotation search criteria and the errors that search can report.

`AnnotationFilter` holds the criteria a client chose, and `OwnershipScope`
holds the ownership restriction that authorization adds; search combines the
two. Search accepts only known query parameters, and closure search (matching
an ontology term and its descendants) needs a supported field, a term, a
predicate loaded for the active ontology, and an active ontology. The errors
here describe each way a search request can fail those rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from standard_annotation_backend.domain.errors import (
    InvalidInputError,
    UnavailableError,
)


class UnknownQueryParameterError(InvalidInputError):
    """Report a query parameter that search does not recognize.

    The message stays fixed; the parameter name appears only in the issue
    location, so client-supplied text is never echoed in the message.

    Attributes:
        name: The unrecognized query parameter.
    """

    code = "unknown_query_parameter"
    message = "Query parameter is not recognized"

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__()

    def issue_location(self) -> tuple[str | int, ...]:
        """Return the location of the unrecognized query parameter."""
        return ("query", self.name)


class UnsupportedFilterError(InvalidInputError):
    """Report a recognized annotation field that search cannot filter on.

    Attributes:
        name: The query parameter naming the unsupported filter.
    """

    code = "unsupported_filter"
    message = "Annotation filter is not supported"

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__()

    def issue_location(self) -> tuple[str | int, ...]:
        """Return the location of the unsupported filter parameter."""
        return ("query", self.name)


class ClosureTermRequiredError(InvalidInputError):
    """Report a closure predicate supplied without an ontology term to expand."""

    code = "closure_term_required"
    message = "ontology_class_id is required for closure search"

    def issue_location(self) -> tuple[str | int, ...]:
        """Return the location of the missing ontology term parameter."""
        return ("query", "ontology_class_id")


class UnsupportedClosureFieldError(InvalidInputError):
    """Report closure search requested for a field other than the ontology class.

    Attributes:
        name: The query parameter requesting closure search.
    """

    code = "unsupported_closure_field"
    message = "Closure search is supported only for ontology_class_id"

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__()

    def issue_location(self) -> tuple[str | int, ...]:
        """Return the location of the unsupported closure parameter."""
        return ("query", self.name)


class UnsupportedClosurePredicateError(InvalidInputError):
    """Report a closure predicate that the active ontology has not loaded.

    Attributes:
        name: The query parameter carrying the predicate.
    """

    code = "unsupported_closure_predicate"
    message = "Closure predicate is not loaded for the active ontology"

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__()

    def issue_location(self) -> tuple[str | int, ...]:
        """Return the location of the predicate parameter."""
        return ("query", self.name)


class OntologyUnavailableError(UnavailableError):
    """Report that closure search has no active ontology to expand terms with."""

    code = "ontology_unavailable"
    message = "The ontology required for closure search is unavailable"


@dataclass(frozen=True, slots=True)
class AnnotationFilter:
    """Describe the field values that matching active annotations must have.

    Each scalar value must match exactly when supplied. Every value in a tuple
    must be present in the corresponding list-valued annotation field. Creating
    a filter enforces that closure search names the ontology term to expand.

    Attributes:
        db_object_id: Database object identifier to match.
        negation: Negation value to match.
        relation: Relation identifier to match.
        ontology_class_id: Ontology class identifier to match.
        ontology_class_id_closure: Loaded predicate that widens the
            `ontology_class_id` match to the term and its descendants.
        references: Reference identifiers that must all be present.
        evidence_type: Evidence type identifier to match.
        with_or_from: Supporting identifiers that must all be present.
        interacting_taxon_id: Taxon identifiers that must all be present.
        annotation_date: Annotation date to match.
        assigned_by: Assigning organization to match.

    Raises:
        ClosureTermRequiredError: If `ontology_class_id_closure` is supplied
            without `ontology_class_id`.
    """

    db_object_id: str | None = None
    negation: bool | None = None
    relation: str | None = None
    ontology_class_id: str | None = None
    ontology_class_id_closure: str | None = None
    references: tuple[str, ...] = ()
    evidence_type: str | None = None
    with_or_from: tuple[str, ...] = ()
    interacting_taxon_id: tuple[str, ...] = ()
    annotation_date: date | None = None
    assigned_by: str | None = None

    def __post_init__(self) -> None:
        if (
            self.ontology_class_id_closure is not None
            and self.ontology_class_id is None
        ):
            raise ClosureTermRequiredError


@dataclass(frozen=True, slots=True)
class OwnershipScope:
    """Restrict a search to annotations the caller's authorization may see.

    An empty scope places no ownership restriction on results.

    Attributes:
        owning_group_id: Group whose annotations may be returned.
        created_by: Actor recorded on the first annotation version that
            returned annotations must have.
    """

    owning_group_id: str | None = None
    created_by: str | None = None
