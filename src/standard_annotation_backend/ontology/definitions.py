"""Define the identifier prefixes and closure predicates of supported ontologies."""

from standard_annotation_backend.domain.ontology import OntologyDefinition, OntologyKey

GO_DEFINITION = OntologyDefinition(
    key=OntologyKey.GO,
    identifier_prefixes=("GO",),
    closure_predicates=("rdfs:subClassOf", "BFO:0000050"),
    document_ontology_id="go",
)

_DEFINITIONS = {OntologyKey.GO: GO_DEFINITION}


def ontology_definition(key: OntologyKey) -> OntologyDefinition:
    """Return the definition of a supported ontology."""
    return _DEFINITIONS[key]
