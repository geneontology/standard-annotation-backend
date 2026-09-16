"""Provide reusable request examples for SAB's OpenAPI documentation."""

from fastapi.openapi.models import Example

STANDARD_ANNOTATION_EXAMPLE: dict[str, object] = {
    "db_object_id": "UniProtKB:P12345",
    "relation": "RO:0002331",
    "ontology_class_id": "GO:0008150",
    "references": ["PMID:12345678"],
    "evidence_type": "ECO:0000314",
    "with_or_from": ["UniProtKB:Q9XYZ1"],
    "interacting_taxon_id": ["NCBITaxon:9606"],
    "annotation_date": "2026-09-15",
    "assigned_by": "GO_Central",
    "annotation_extensions": [
        {
            "extension_relation": "BFO:0000050",
            "extension_term": "CL:0000000",
        }
    ],
    "annotation_properties": {
        "comment": ["Curated from the cited publication"],
    },
}

ANNOTATION_CREATE_EXAMPLES: dict[str, Example] = {
    "create-annotation": {
        "summary": "Create a standard annotation",
        "value": {
            "owning_group_id": "GO_Central",
            "annotation": STANDARD_ANNOTATION_EXAMPLE,
        },
    }
}

ANNOTATION_PATCH_EXAMPLES: dict[str, Example] = {
    "replace-annotation-fields": {
        "summary": "Replace complete annotation fields",
        "description": (
            "List-valued fields are unordered and must be replaced as complete arrays."
        ),
        "value": {
            "references": ["PMID:12345678", "GO_REF:0000002"],
            "annotation_date": "2026-09-15",
        },
    }
}

_EXAMPLE_ANNOTATION_ID = "018f6fa8-709b-7c13-9f75-4513f57b98c2"

CHANGE_SET_PROPOSAL_EXAMPLES: dict[str, Example] = {
    "create": {
        "summary": "Propose a new annotation",
        "value": {
            "operation": "create",
            "owning_group_id": "GO_Central",
            "annotation": STANDARD_ANNOTATION_EXAMPLE,
            "reason": "Add annotation supported by the cited publication",
        },
    },
    "update": {
        "summary": "Propose an annotation update",
        "description": (
            "Replace unordered list fields as complete arrays rather than addressing "
            "elements by position."
        ),
        "value": {
            "operation": "update",
            "annotation_id": _EXAMPLE_ANNOTATION_ID,
            "base_version": 3,
            "patch_format": "application/json-patch+json",
            "patch": [
                {
                    "op": "replace",
                    "path": "/references",
                    "value": ["PMID:12345678", "GO_REF:0000002"],
                }
            ],
            "reason": "Add the supporting GO reference",
        },
    },
    "delete": {
        "summary": "Propose annotation deletion",
        "value": {
            "operation": "delete",
            "annotation_id": _EXAMPLE_ANNOTATION_ID,
            "base_version": 3,
            "reason": "Withdraw an annotation that is no longer supported",
        },
    },
}

CHANGE_SET_ACCEPT_EXAMPLES: dict[str, Example] = {
    "accept": {
        "summary": "Accept a reviewed proposal",
        "value": {
            "review_reason": "Verified against the cited source evidence",
        },
    }
}

CHANGE_SET_REJECT_EXAMPLES: dict[str, Example] = {
    "reject": {
        "summary": "Reject a reviewed proposal",
        "value": {
            "review_reason": "The evidence does not support the proposed change",
        },
    }
}
