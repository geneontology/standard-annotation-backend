"""Name the shape of each issue's `details`.

Some issues carry `details`: JSON-compatible facts that help fix the problem, such
as an obsolete term's suggested replacements. Each rule in `rules.RULES` declares
which of these shapes its issues use, and the API publishes each shape as a named
schema. A shape rejects keys it does not declare when Pydantic validates it, so a
details value that matches no published shape fails instead of losing keys.
"""

from typing import TypedDict

from pydantic import ConfigDict, with_config

_EXACT = ConfigDict(extra="forbid")


@with_config(_EXACT)
class ObsoleteTermDetails(TypedDict):
    """Suggest terms to use instead of an obsolete term.

    Attributes:
        replaced_by: The terms the ontology says replace it, usually one.
        consider: Terms the ontology suggests considering instead.
    """

    replaced_by: list[str]
    consider: list[str]


@with_config(_EXACT)
class PrimaryIdDetails(TypedDict):
    """Name the primary identifier of an alternative identifier.

    Attributes:
        primary_id: The identifier to use instead.
    """

    primary_id: str


@with_config(_EXACT)
class DuplicatePeersDetails(TypedDict):
    """List the other active annotations an annotation duplicates.

    Attributes:
        peer_ids: The duplicate annotations' IDs.
    """

    peer_ids: list[str]


@with_config(_EXACT)
class AllowedRelationsDetails(TypedDict):
    """List the relations allowed for the annotated term.

    Attributes:
        allowed_relations: The allowed relation identifiers, sorted.
    """

    allowed_relations: list[str]


IssueDetails = (
    ObsoleteTermDetails
    | PrimaryIdDetails
    | DuplicatePeersDetails
    | AllowedRelationsDetails
)
"""Any issue's `details`."""
