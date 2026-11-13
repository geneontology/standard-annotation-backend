"""Define the checked ancestors: the GO terms that GO Rules test other terms against.

Many rules ask whether a term is under a particular GO term. A term is under
another when it is that term or one of its `rdfs:subClassOf` descendants in GO.
Before the rules run, SABE looks up which checked ancestors each current GO term in
an annotation falls under, and which ones the subject entity's type falls under.
Only this small set of terms is looked up, so the rules never need the whole GO
hierarchy.
"""

from standard_annotation_backend.domain.semantic_validation.rule_data import RuleData

MF_ROOT = "GO:0003674"
"""The root term of GO's molecular function terms."""

BP_ROOT = "GO:0008150"
"""The root term of GO's biological process terms."""

CC_ROOT = "GO:0005575"
"""The root term of GO's cellular component terms."""

GO_ROOTS = frozenset({MF_ROOT, BP_ROOT, CC_ROOT})
"""The three GO roots."""

CATALYTIC_ACTIVITY = "GO:0003824"
PROTEIN_CONTAINING_COMPLEX = "GO:0032991"
CELLULAR_ANATOMICAL_STRUCTURE = "GO:0110165"
VIRION_COMPONENT = "GO:0044423"

INTERSPECIES_INTERACTION_TERMS = frozenset({"GO:0044419", "GO:0043903", "GO:0018995"})
"""Terms under which an annotation may name interacting taxa (GORULE:0000015)."""

SELF_BINDING_TERMS = frozenset(
    {
        "GO:0042803",
        "GO:0051260",
        "GO:0051289",
        "GO:0070207",
        "GO:0043621",
        "GO:0032840",
    }
)
"""Terms whose `with_or_from` values must include the subject (GORULE:0000046)."""

FIXED_CHECKED_ANCESTORS = (
    GO_ROOTS
    | {
        CATALYTIC_ACTIVITY,
        PROTEIN_CONTAINING_COMPLEX,
        CELLULAR_ANATOMICAL_STRUCTURE,
        VIRION_COMPONENT,
    }
    | INTERSPECIES_INTERACTION_TERMS
    | SELF_BINDING_TERMS
)
"""Every checked ancestor the rules name directly."""


def checked_ancestor_terms(rule_data: RuleData) -> frozenset[str]:
    """Return the fixed checked ancestors plus every extension constraint's root terms.

    GORULE:0000058 asks whether the annotated term falls under an extension
    constraint's `primary_root_terms`. Those terms come from the go-site rule data,
    so they are added here rather than listed in `FIXED_CHECKED_ANCESTORS`.
    """
    return FIXED_CHECKED_ANCESTORS | {
        root
        for constraint in rule_data.extension_constraints
        for root in constraint.primary_root_terms
    }
