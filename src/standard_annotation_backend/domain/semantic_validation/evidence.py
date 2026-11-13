"""Look up the GO evidence code for an annotation's evidence term.

Annotations record evidence as an ECO term, such as `ECO:0000353`, while GO Rules
refer to evidence by its GO evidence code, such as `IPI`. The schema allows exactly
one ECO term for each code, so every allowed term has one code.
"""

from collections.abc import Mapping
from functools import cache
from types import MappingProxyType

from standard_annotation_backend.domain.schema_artifacts import load_evidence_codes


@cache
def _evidence_codes() -> Mapping[str, str]:
    """Return each allowed evidence term's GO evidence code, loaded on first use."""
    return MappingProxyType(load_evidence_codes())


def go_evidence_code(evidence_type: str) -> str:
    """Return the GO evidence code for an evidence term.

    GO Rules use this to tell which kind of evidence supports an annotation, for
    example whether an annotation is based on IPI.

    Args:
        evidence_type: The `evidence_type` of an annotation that passed schema
            validation, such as `ECO:0000353`.

    Returns:
        The GO evidence code, such as `IPI`.

    Raises:
        ValueError: If `evidence_type` is not an evidence term the schema allows.
            Schema validation rejects such terms, so a validated annotation never
            causes this error.
        SchemaArtifactError: If the packaged evidence value set is malformed.
    """
    try:
        return _evidence_codes()[evidence_type]
    except KeyError:
        raise ValueError(
            f"{evidence_type} is not an evidence term the schema allows"
        ) from None
