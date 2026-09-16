"""Typed request and response models for annotation and change-set APIs."""

from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from fastapi import Body
from pydantic import BaseModel, ConfigDict, Field

from standard_annotation_backend.api.examples import CHANGE_SET_PROPOSAL_EXAMPLES
from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationExtension,
    AnnotationProperties,
)
from standard_annotation_backend.services.annotation_service import (
    AnnotationVersion,
    CurrentAnnotation,
    ResultPage,
)
from standard_annotation_backend.services.change_set_service import (
    AcceptedChangeSet,
    ChangeSet,
    ChangeSetPreview,
)


class ChangeSetCreateRequest(BaseModel):
    """Propose raw annotation data whose validity will be assessed during review."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["create"]
    owning_group_id: Annotated[
        str,
        Field(pattern=r".*\S.*", examples=["GO_Central"]),
    ]
    annotation: dict[str, object]
    reason: Annotated[str, Field(pattern=r".*\S.*")]


class ChangeSetUpdateRequest(BaseModel):
    """Propose a nonempty JSON Patch against a saved annotation version."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["update"]
    annotation_id: UUID
    base_version: Annotated[int, Field(gt=0, strict=True)]
    patch_format: Literal["application/json-patch+json"] = "application/json-patch+json"
    patch: Annotated[list[object], Field(min_length=1)]
    reason: Annotated[str, Field(pattern=r".*\S.*")]


class ChangeSetDeleteRequest(BaseModel):
    """Propose deletion of an annotation based on a saved version."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["delete"]
    annotation_id: UUID
    base_version: Annotated[int, Field(gt=0, strict=True)]
    reason: Annotated[str, Field(pattern=r".*\S.*")]


type ChangeSetProposalRequest = Annotated[
    ChangeSetCreateRequest | ChangeSetUpdateRequest | ChangeSetDeleteRequest,
    Body(
        discriminator="operation",
        openapi_examples=CHANGE_SET_PROPOSAL_EXAMPLES,
    ),
]


class ChangeSetAcceptRequest(BaseModel):
    """Supply optional reviewer text when accepting a proposal."""

    model_config = ConfigDict(extra="forbid")

    review_reason: str | None = None


class ChangeSetRejectRequest(BaseModel):
    """Require a nonblank explanation when rejecting a proposal."""

    model_config = ConfigDict(extra="forbid")

    review_reason: Annotated[str, Field(pattern=r".*\S.*")]


class AnnotationCreateRequest(BaseModel):
    """Describe a request to create an annotation through the API.

    Attributes:
        owning_group_id: Identifier of the group responsible for the annotation.
        annotation: Validated Standard Annotation data to create.
    """

    model_config = ConfigDict(extra="forbid")

    owning_group_id: Annotated[
        str,
        Field(pattern=r".*\S.*", examples=["GO_Central"]),
    ]
    annotation: Annotation


class AnnotationPatchRequest(BaseModel):
    """Hold the annotation fields supplied in a partial update.

    A field is changed only when it appears in the request. A supplied list replaces
    the complete existing list rather than adding or removing individual entries.
    """

    model_config = ConfigDict(extra="forbid")

    db_object_id: str | None = None
    negation: bool | None = None
    relation: str | None = None
    ontology_class_id: str | None = None
    references: list[str] | None = None
    evidence_type: str | None = None
    with_or_from: list[str] | None = None
    interacting_taxon_id: list[str] | None = None
    annotation_date: date | None = None
    assigned_by: str | None = None
    annotation_extensions: list[AnnotationExtension] | None = None
    annotation_properties: AnnotationProperties | None = None

    def changes(self) -> dict[str, object]:
        """Return only the fields that the client included in the request.

        Returns:
            Top-level field names mapped to their JSON-compatible replacement values.
        """
        return self.model_dump(mode="json", exclude_unset=True)


class AnnotationResource(BaseModel):
    """Represent the latest active state of an annotation in an API response."""

    annotation_id: UUID
    version: int
    owning_group_id: str
    record_origin: str
    source_import_job_id: UUID | None
    created_at: datetime
    updated_at: datetime
    annotation: Annotation

    @classmethod
    def from_service(cls, result: CurrentAnnotation) -> Self:
        """Build an API response from the service's current annotation data.

        Args:
            result: Current annotation returned by the application service.

        Returns:
            The same data represented as a validated API response model.
        """
        return cls(
            annotation_id=result.annotation_id,
            version=result.version,
            owning_group_id=result.owning_group_id,
            record_origin=result.record_origin,
            source_import_job_id=result.source_import_job_id,
            created_at=result.created_at,
            updated_at=result.updated_at,
            annotation=result.annotation,
        )


class AnnotationVersionResource(BaseModel):
    """Represent one saved annotation version in an API response."""

    annotation_id: UUID
    version: int
    is_deleted: bool
    actor_id: str
    change_source: str
    created_at: datetime
    annotation: Annotation

    @classmethod
    def from_service(cls, result: AnnotationVersion) -> Self:
        """Build an API response from one saved annotation version.

        Args:
            result: Saved version returned by the application service.

        Returns:
            The saved version represented as a validated API response model.
        """
        return cls(
            annotation_id=result.annotation_id,
            version=result.version,
            is_deleted=result.is_deleted,
            actor_id=result.actor_id,
            change_source=result.change_source,
            created_at=result.created_at,
            annotation=result.annotation,
        )


class AnnotationPageResponse(BaseModel):
    """Represent one requested page of active annotations."""

    items: list[AnnotationResource]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_service(cls, result: ResultPage[CurrentAnnotation]) -> Self:
        """Build an API response from a page of current annotations.

        Args:
            result: Current annotations and pagination data from the service.

        Returns:
            A validated page suitable for an API response.
        """
        return cls(
            items=[AnnotationResource.from_service(item) for item in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        )


class AnnotationVersionPageResponse(BaseModel):
    """Represent one requested page of saved annotation versions."""

    items: list[AnnotationVersionResource]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_service(cls, result: ResultPage[AnnotationVersion]) -> Self:
        """Build an API response from a page of saved annotation versions.

        Args:
            result: Saved versions and pagination data from the service.

        Returns:
            A validated page suitable for an API response.
        """
        return cls(
            items=[
                AnnotationVersionResource.from_service(item) for item in result.items
            ],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        )


class ApiValidationIssue(BaseModel):
    """Describe one invalid value in a request or annotation."""

    location: tuple[str | int, ...]
    message: str
    type: str


class ChangeSetPreviewResource(BaseModel):
    """Show the candidate annotation and current obstacles to acceptance.

    A preview is advisory. Acceptance repeats validation and concurrency checks.
    """

    model_config = ConfigDict(from_attributes=True)

    change_set_id: UUID
    operation: Literal["create", "update", "delete"]
    annotation_id: UUID | None
    base_version: int | None
    current_version: int | None
    before_annotation: dict[str, object] | None
    annotation: dict[str, object] | None
    is_deleted: bool
    validation_errors: tuple[ApiValidationIssue, ...]
    duplicate_peer_ids: tuple[UUID, ...]
    is_stale: bool
    can_accept: bool

    @classmethod
    def from_service(cls, result: ChangeSetPreview) -> Self:
        """Convert a service preview to the public response model."""
        return cls.model_validate(result)


class ChangeSetResource(BaseModel):
    """Expose a proposal, its last stored preview, and its review outcome."""

    model_config = ConfigDict(from_attributes=True)

    change_set_id: UUID
    operation: Literal["create", "update", "delete"]
    state: Literal["proposed", "accepted", "rejected", "stale"]
    owning_group_id: str
    annotation_id: UUID | None
    base_version: int | None
    annotation_payload: dict[str, object] | None
    patch: list[dict[str, object]] | None
    reason: str
    preview: ChangeSetPreviewResource | None
    previewed_at: datetime | None
    proposed_by: str
    proposed_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_reason: str | None
    result_annotation_version: int | None

    @classmethod
    def from_service(cls, result: ChangeSet) -> Self:
        """Convert a service proposal and its stored preview to an API resource."""
        return cls.model_validate(result)


class AcceptedChangeSetResource(BaseModel):
    """Identify an accepted proposal and its resulting annotation version."""

    model_config = ConfigDict(from_attributes=True)

    change_set: ChangeSetResource
    annotation_id: UUID
    version: int
    is_deleted: bool
    annotation: Annotation

    @classmethod
    def from_service(cls, result: AcceptedChangeSet) -> Self:
        """Convert acceptance data, including deletion results, to an API response."""
        return cls.model_validate(result)


class DuplicateAnnotationDetails(BaseModel):
    """List active annotations that conflict with a requested change."""

    peer_ids: tuple[UUID, ...]


class StaleAnnotationVersionDetails(BaseModel):
    """Explain that an annotation changed after the client last read it."""

    annotation_id: UUID
    expected_version: int
    current_version: int


class StaleChangeSetDetails(BaseModel):
    """Identify a stale proposal and the annotation versions that conflicted."""

    change_set_id: UUID
    annotation_id: UUID
    expected_version: int
    current_version: int


type ApiErrorDetails = (
    list[ApiValidationIssue]
    | DuplicateAnnotationDetails
    | StaleAnnotationVersionDetails
    | StaleChangeSetDetails
)


class ApiErrorBody(BaseModel):
    """Provide machine-readable and human-readable information about an error."""

    code: str
    message: str
    details: ApiErrorDetails | None = None


class ApiErrorResponse(BaseModel):
    """Wrap every expected API error in a consistent top-level object."""

    error: ApiErrorBody
