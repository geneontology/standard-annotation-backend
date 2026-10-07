"""Define the errors that annotation search can report.

Search accepts only known query parameters, and closure search (matching an
ontology term and its descendants) needs a supported field, a term, a predicate
loaded for the active ontology, and an active ontology. These errors describe
each way a search request can fail those rules.
"""

from __future__ import annotations

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
