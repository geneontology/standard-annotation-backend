"""Define typed request and response models for SABE's HTTP APIs.

Each resource that represents a service result converts it through `from_service`.
Conversion reads only the attributes the resource declares, so fields added to a
service result do not appear in API responses until the resource declares them.
Page constructors convert each item through its resource constructor.
"""

from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from fastapi import Body
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, WithJsonSchema

from standard_annotation_backend.api.dependencies import PageLimit, PageOffset
from standard_annotation_backend.api.examples import CHANGE_SET_PROPOSAL_EXAMPLES
from standard_annotation_backend.domain.annotation_search import AnnotationFilter
from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationExtension,
    AnnotationProperties,
)
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
)
from standard_annotation_backend.domain.change_sets import (
    ChangeSetOperation,
    ChangeSetState,
)
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.qc_reports import (
    ISSUE_TYPES,
    STORED_QC_REPORT_RESULT,
    IssueTypeEntry,
    QcFinding,
)
from standard_annotation_backend.domain.semantic_validation.details import (
    IssueDetails,
)
from standard_annotation_backend.domain.tokens import TokenMetadata
from standard_annotation_backend.refresh.sources import SourceKey
from standard_annotation_backend.services.annotation_service import (
    AnnotationVersion,
    CurrentAnnotation,
)
from standard_annotation_backend.services.change_set_service import (
    AcceptedChangeSet,
    ChangeSet,
    ChangeSetPreview,
)
from standard_annotation_backend.services.comment_service import AnnotationComment
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.pagination import ResultPage
from standard_annotation_backend.services.qc_report_service import (
    QcReportListing,
    QcReportSummary,
)
from standard_annotation_backend.services.token_service import (
    CreatedToken,
    TokenContext,
    TokenCreateInput,
)
from standard_annotation_backend.validation_types import (
    ExactNonBlankString,
    NonBlankString,
)


class ChangeSetCreateRequest(BaseModel):
    """Propose raw annotation data whose validity will be assessed during review."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["create"]
    owning_group_id: Annotated[
        NonBlankString | None,
        Field(
            examples=["GO_Central"],
            description="Uses the token's selected group when omitted; global tokens must supply an explicit group.",
        ),
    ] = None
    annotation: dict[str, object]
    reason: NonBlankString


class ChangeSetUpdateRequest(BaseModel):
    """Propose a nonempty JSON Patch against a saved annotation version."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["update"]
    annotation_id: UUID
    base_version: Annotated[int, Field(gt=0, strict=True)]
    patch_format: Literal["application/json-patch+json"] = "application/json-patch+json"
    patch: Annotated[list[object], Field(min_length=1)]
    reason: NonBlankString


class ChangeSetDeleteRequest(BaseModel):
    """Propose deletion of an annotation based on a saved version."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["delete"]
    annotation_id: UUID
    base_version: Annotated[int, Field(gt=0, strict=True)]
    reason: NonBlankString


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

    review_reason: NonBlankString


class AnnotationCreateRequest(BaseModel):
    """Describe a request to create an annotation through the API.

    Attributes:
        owning_group_id: Identifier of the group responsible for the annotation.
        annotation: Validated Standard Annotation data to create.
    """

    model_config = ConfigDict(extra="forbid")

    owning_group_id: Annotated[
        NonBlankString | None,
        Field(
            examples=["GO_Central"],
            description="Uses the token's selected group when omitted; global tokens must supply an explicit group.",
        ),
    ] = None
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


class AnnotationSearchQuery(BaseModel):
    """Describe the query parameters that filter an annotation search.

    Each scalar parameter must match exactly. Repeating a list-valued parameter
    requires every supplied value to be present. Unknown parameter names are not
    rejected here; the search route reports them with its own error codes.

    Attributes:
        db_object_id: Database object identifier to match.
        negation: Negation value to match.
        relation: Relation identifier to match.
        ontology_class_id: Ontology class identifier to match.
        ontology_class_id_closure: Loaded predicate for descendant-or-self matching.
        references: Reference identifiers that must all be present.
        evidence_type: Evidence type identifier to match.
        with_or_from: Supporting identifiers that must all be present.
        interacting_taxon_id: Taxon identifiers that must all be present.
        annotation_date: Annotation date to match.
        assigned_by: Assigning organization to match.
    """

    db_object_id: str | None = None
    negation: bool | None = None
    relation: str | None = None
    ontology_class_id: str | None = None
    ontology_class_id_closure: Annotated[
        str | None,
        Field(
            description=(
                "Return annotations whose ontology class is a descendant-or-self "
                "of `ontology_class_id` through this loaded predicate."
            )
        ),
    ] = None
    references: list[str] | None = None
    evidence_type: str | None = None
    with_or_from: list[str] | None = None
    interacting_taxon_id: list[str] | None = None
    annotation_date: date | None = None
    assigned_by: str | None = None

    def to_filter(self) -> AnnotationFilter:
        """Return these parameters as annotation search criteria.

        Call this from the route body rather than during query parsing so that
        an invalid combination is reported with its own error code instead of as
        a generic request-validation failure.

        Returns:
            Criteria with omitted list parameters represented as empty tuples.

        Raises:
            ClosureTermRequiredError: If `ontology_class_id_closure` is supplied
                without `ontology_class_id`.
        """
        return AnnotationFilter(
            db_object_id=self.db_object_id,
            negation=self.negation,
            relation=self.relation,
            ontology_class_id=self.ontology_class_id,
            ontology_class_id_closure=self.ontology_class_id_closure,
            references=tuple(self.references or ()),
            evidence_type=self.evidence_type,
            with_or_from=tuple(self.with_or_from or ()),
            interacting_taxon_id=tuple(self.interacting_taxon_id or ()),
            annotation_date=self.annotation_date,
            assigned_by=self.assigned_by,
        )


class AnnotationListQuery(AnnotationSearchQuery):
    """Describe every query parameter accepted by the annotation search route.

    FastAPI documents a query-parameter model as individual parameters only when
    it is the route's sole query input, so the pagination parameters are part of
    this model rather than separate route arguments.

    Attributes:
        limit: Maximum number of annotations to return.
        offset: Number of matching annotations to skip.
    """

    limit: PageLimit = 50
    offset: PageOffset = 0


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
        return cls.model_validate(result, from_attributes=True)


class JobResource(BaseModel):
    """Represent job status and results returned by the API."""

    job_id: UUID
    job_type: JobType
    status: JobStatus
    requested_by: str
    progress: dict[str, object] = Field(
        description=(
            "Type-specific progress. Failed refresh jobs include phase='failed', "
            "a failure_code, and, when available, failure_details describing the "
            "problem: a message for an invalid header, or issue_count and up to "
            "100 issues (line number, category, and invalid fields) for invalid "
            "rows. Completed refresh jobs, except entity retirement, include "
            "unchanged, which is true when the source document was already "
            "active so nothing was applied. Term replacement and QC report jobs "
            "include a phase and running counts; when they fail, they include "
            "phase='failed' and a failure_code."
        ),
        examples=[
            {
                "phase": "failed",
                "failure_code": "row_validation",
                "failure_details": {
                    "issue_count": 1,
                    "issues": [
                        {
                            "line_number": 412,
                            "category": "validation",
                            "message": None,
                            "fields": [
                                {
                                    "field": "db_object_symbol",
                                    "message": (
                                        "Value error, Invalid db_object_symbol "
                                        "format: A raw rejected symbol"
                                    ),
                                    "value": "A raw rejected symbol",
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    )
    warnings: tuple[str, ...]
    result: dict[str, object] | None = Field(
        description=(
            "Type-specific durable result. Successful results of every refresh "
            "job, except entity retirement, include unchanged, true when "
            "nothing was applied. Entity refresh results also include "
            "warning_count and warnings."
        ),
        examples=[{"unchanged": False, "warning_count": 0, "warnings": []}],
    )
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_service(cls, job: Job) -> Self:
        """Convert a `JobService` result; worker parameters are not included."""
        return cls.model_validate(job, from_attributes=True)


class RefreshRequest(BaseModel):
    """Request a refresh of one configured source, or of every source of a kind."""

    model_config = ConfigDict(extra="forbid")

    source_key: Annotated[
        SourceKey | None,
        Field(
            description=(
                "Key of a source configured in config/sources.yaml. Omit it to "
                "refresh every configured source of this kind."
            ),
        ),
    ] = None


class AnnotationCutoverRequest(BaseModel):
    """Request the final GPAD import that moves one group to SABE management."""

    model_config = ConfigDict(extra="forbid")

    source_key: Annotated[
        SourceKey,
        Field(
            description=(
                "Key of an annotation source configured in config/sources.yaml. Its "
                "group moves to SABE management permanently if the import succeeds."
            ),
            examples=["mgi"],
        ),
    ]


class RefreshJobsResource(BaseModel):
    """List the jobs created or reused by a refresh request."""

    jobs: list[JobResource]


class AnnotationCommentRequest(BaseModel):
    """Supply the text for a new or edited annotation comment."""

    model_config = ConfigDict(extra="forbid")

    body: NonBlankString


class AnnotationCommentResource(BaseModel):
    """Represent a comment attached to a specific annotation version."""

    comment_id: UUID
    annotation_id: UUID
    annotation_version: int
    body: str
    created_by: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_service(cls, result: AnnotationComment) -> Self:
        """Build a response from a comment service result.

        Args:
            result: Comment returned by the application service.

        Returns:
            Serialized public comment resource.
        """
        return cls.model_validate(result, from_attributes=True)


class PageResource[T](BaseModel):
    """Represent one requested page of API resources.

    Each paginated endpoint declares its own subclass with a concrete item type, so
    every page keeps its own name and description in the OpenAPI document.

    Attributes:
        items: Resources included in this page.
        total: Number of matching resources across all pages.
        limit: Maximum number of resources requested for this page.
        offset: Number of matching resources skipped before this page.
    """

    items: list[T]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_result_page[S](
        cls, result: ResultPage[S], convert: Callable[[S], T]
    ) -> Self:
        """Build a response page by converting each service result.

        Args:
            result: Page returned by an application service.
            convert: Resource constructor applied to each result in order.

        Returns:
            A validated page with the converted items and the service's pagination
            values.
        """
        return cls(
            items=[convert(item) for item in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        )


class AnnotationCommentPageResponse(PageResource[AnnotationCommentResource]):
    """Represent one requested page of visible annotation comments."""

    @classmethod
    def from_service(cls, result: ResultPage[AnnotationComment]) -> Self:
        """Build a response page from a comment service result.

        Args:
            result: Page returned by the application service.

        Returns:
            Serialized public comment page.
        """
        return cls.from_result_page(result, AnnotationCommentResource.from_service)


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
        return cls.model_validate(result, from_attributes=True)


class AnnotationPageResponse(PageResource[AnnotationResource]):
    """Represent one requested page of active annotations."""

    @classmethod
    def from_service(cls, result: ResultPage[CurrentAnnotation]) -> Self:
        """Build an API response from a page of current annotations.

        Args:
            result: Current annotations and pagination data from the service.

        Returns:
            A validated page suitable for an API response.
        """
        return cls.from_result_page(result, AnnotationResource.from_service)


class AnnotationVersionPageResponse(PageResource[AnnotationVersionResource]):
    """Represent one requested page of saved annotation versions."""

    @classmethod
    def from_service(cls, result: ResultPage[AnnotationVersion]) -> Self:
        """Build an API response from a page of saved annotation versions.

        Args:
            result: Saved versions and pagination data from the service.

        Returns:
            A validated page suitable for an API response.
        """
        return cls.from_result_page(result, AnnotationVersionResource.from_service)


class ApiValidationIssue(BaseModel):
    """Describe one invalid value in a request or annotation.

    `details` gives more information about the problem, such as the suggested
    replacements for an obsolete term or the IDs of duplicate annotations. It is
    omitted when there is none.
    """

    location: tuple[str | int, ...]
    message: str
    type: str
    details: IssueDetails | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Facts that help fix the problem. The issue's `type` decides which "
            "shape they have; the issue-type catalog of the QC report findings "
            "route names the shape for each type. Absent when there are none, "
            "and always absent for request issues."
        ),
    )


class ChangeSetPreviewResource(BaseModel):
    """Show the candidate annotation and current obstacles to acceptance.

    A preview is advisory. Acceptance repeats validation and concurrency checks.
    """

    change_set_id: UUID
    operation: ChangeSetOperation
    annotation_id: UUID | None
    base_version: int | None
    current_version: int | None
    before_annotation: dict[str, object] | None
    annotation: dict[str, object] | None
    is_deleted: bool
    validation_errors: tuple[ApiValidationIssue, ...]
    is_stale: bool
    can_accept: bool

    @classmethod
    def from_service(cls, result: ChangeSetPreview) -> Self:
        """Convert a service preview to the public response model."""
        return cls.model_validate(result, from_attributes=True)


class ChangeSetResource(BaseModel):
    """Expose a proposal, its last stored preview, and its review outcome."""

    change_set_id: UUID
    operation: ChangeSetOperation
    state: ChangeSetState
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
        return cls.model_validate(result, from_attributes=True)


class AcceptedChangeSetResource(BaseModel):
    """Identify an accepted proposal and its resulting annotation version."""

    change_set: ChangeSetResource
    annotation_id: UUID
    version: int
    is_deleted: bool
    annotation: Annotation

    @classmethod
    def from_service(cls, result: AcceptedChangeSet) -> Self:
        """Convert acceptance data, including deletion results, to an API response."""
        return cls.model_validate(result, from_attributes=True)


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
    list[ApiValidationIssue] | StaleAnnotationVersionDetails | StaleChangeSetDetails
)


class ApiErrorBody(BaseModel):
    """Provide machine-readable and human-readable information about an error."""

    code: str
    message: str
    details: ApiErrorDetails | None = None


class ApiErrorResponse(BaseModel):
    """Wrap every expected API error in a consistent top-level object."""

    error: ApiErrorBody


class TokenCreateRequest(TokenCreateInput):
    """Name a token, choose an owned context, and provide its expiration timestamp."""


class TokenContextResource(BaseModel):
    """Expose one current context available to the authenticated user."""

    assignment_id: UUID
    role: AuthorizationRole
    scope: AuthorizationScope
    group_id: str | None

    @classmethod
    def from_service(cls, context: TokenContext) -> Self:
        """Build a response from a context the user may select for a token."""
        return cls.model_validate(context, from_attributes=True)


class TokenContextListResponse(BaseModel):
    """List the user's active authorization choices."""

    items: list[TokenContextResource]


class TokenResource(BaseModel):
    """Expose token metadata without a bearer secret or lookup digest."""

    token_id: UUID
    user_id: UUID
    assignment_id: UUID
    name: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    role: AuthorizationRole
    scope: AuthorizationScope
    group_id: str | None
    assignment_is_active: bool

    @classmethod
    def from_service(cls, metadata: TokenMetadata) -> Self:
        """Build a response from token metadata, which never includes the secret."""
        return cls.model_validate(metadata, from_attributes=True)


class TokenCreatedResponse(TokenResource):
    """Return a bearer secret exactly once, when its token is created."""

    token: str = Field(
        repr=False, description="Copy this secret now; it cannot be retrieved again."
    )

    @classmethod
    def from_service(  # ty: ignore[invalid-method-override] -- Creation has its own result type.
        cls, created: CreatedToken
    ) -> Self:
        """Build the creation response, the only one to include the secret."""
        return cls.model_validate(
            {
                "token": created.raw_token,
                **TokenResource.from_service(created.metadata).model_dump(),
            }
        )


class TokenListResponse(BaseModel):
    """List the user's token history with safe metadata only."""

    items: list[TokenResource]


class QcReportRequest(BaseModel):
    """Choose the scope of a QC report."""

    model_config = ConfigDict(extra="forbid")

    owning_group_id: Annotated[
        ExactNonBlankString | None,
        Field(
            description=(
                "Report on this group only. Global admins may omit it to report on "
                "all groups. A group admin's report always covers its own group, "
                "and naming a different group is refused."
            ),
        ),
    ] = None


_PARTIAL_DETAILS = {"gorule_0000030": "only for an alternative GO_REF"}
"""Issue types that send details for only some of their issues."""


def _issue_type_description(entry: IssueTypeEntry) -> str:
    """Return an issue type's title, GO Rule link, and details shape, if any."""
    notes = [] if entry.link is None else [entry.link]
    if entry.details is not None:
        shape = f"details: {entry.details.__name__}"
        when = _PARTIAL_DETAILS.get(entry.issue_type)
        notes.append(shape if when is None else f"{shape}, {when}")
    return entry.title if not notes else f"{entry.title} ({'; '.join(notes)})"


def _cataloged_issue_type(value: str) -> str:
    """Return `value` if it is an issue type a QC report counts."""
    if value not in {entry.issue_type for entry in ISSUE_TYPES}:
        raise ValueError("Unknown issue type")
    return value


IssueTypeParameter = Annotated[
    str,
    AfterValidator(_cataloged_issue_type),
    WithJsonSchema(
        {
            "type": "string",
            "title": "IssueType",
            "enum": [entry.issue_type for entry in ISSUE_TYPES],
            "x-enum-descriptions": [
                _issue_type_description(entry) for entry in ISSUE_TYPES
            ],
            "description": (
                "An issue type a QC report counts. `structural` covers every "
                "issue found by Standard Annotation schema validation; GO Rule "
                "types link to the rule."
            ),
        }
    ),
]
"""An issue type, documented with each value's title and GO Rule link."""


class QcFindingQuery(BaseModel):
    """Filter and page a report's findings."""

    model_config = ConfigDict(extra="forbid")

    issue_type: Annotated[
        IssueTypeParameter | None,
        Field(description="Only findings with at least one issue of this type."),
    ] = None
    owning_group_id: Annotated[
        NonBlankString | None,
        Field(description="Only findings for this group's annotations."),
    ] = None
    limit: PageLimit = 50
    offset: PageOffset = 0


class QcGroupCountsResource(BaseModel):
    """Count one group's annotations in a report."""

    annotations_checked: int
    annotations_with_issues: int


class QcOntologySnapshotResource(BaseModel):
    """Identify one ontology snapshot a report read."""

    ontology_key: str
    version_id: UUID
    document_version: str | None
    source_revision: str | None


class QcEntityCatalogResource(BaseModel):
    """Identify one entity catalog snapshot a report read."""

    source_key: str
    snapshot_id: UUID


class QcReferenceDataResource(BaseModel):
    """Identify one set of reference data a report validated against."""

    ontologies: list[QcOntologySnapshotResource]
    go_site_revision: str | None
    entity_catalogs: list[QcEntityCatalogResource]


class QcReportResource(BaseModel):
    """Describe a QC report: status, scope, timing, versions, and counts.

    Counts cover only the groups the reader may see. While a report runs, they
    are the counts recorded so far.
    """

    report_id: UUID
    status: JobStatus
    owning_group_id: str | None = Field(
        description="The group the report covers, or null for all groups."
    )
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None
    annotations_checked: int
    annotations_with_issues: int
    by_issue_type: dict[str, int] = Field(
        description="The number of annotations with each issue type, by type."
    )
    by_group: dict[str, QcGroupCountsResource]
    reference_data: list[QcReferenceDataResource] = Field(
        description=(
            "Every set of reference data the report used, in the order first "
            "used. Empty until the report succeeds."
        )
    )
    sabe_version: str | None = Field(
        description="The SABE release that ran the report, once it succeeds."
    )
    schema_version: str | None = Field(
        description="The Standard Annotation schema version, once it succeeds."
    )

    @classmethod
    def from_service(cls, summary: QcReportSummary) -> Self:
        """Convert a service summary to the public resource."""
        result = summary.result
        stored = None if result is None else STORED_QC_REPORT_RESULT.dump(result)
        return cls.model_validate(
            {
                "report_id": summary.job.job_id,
                "status": summary.job.status,
                "owning_group_id": summary.owning_group_id,
                "requested_by": summary.job.requested_by,
                "created_at": summary.job.created_at,
                "started_at": summary.job.started_at,
                "completed_at": summary.job.completed_at,
                "error": summary.job.error,
                "annotations_checked": summary.annotations_checked,
                "annotations_with_issues": summary.annotations_with_issues,
                "by_issue_type": dict(summary.by_issue_type),
                "by_group": {
                    group: {
                        "annotations_checked": counts.annotations_checked,
                        "annotations_with_issues": counts.annotations_with_issues,
                    }
                    for group, counts in summary.by_group.items()
                },
                "reference_data": [] if stored is None else stored["reference_data"],
                "sabe_version": None if result is None else result.sabe_version,
                "schema_version": None if result is None else result.schema_version,
            }
        )


class QcFindingResource(BaseModel):
    """Describe one annotation's issues in a report, in the same format as writes."""

    annotation_id: UUID
    annotation_version: int = Field(description="The version that was checked.")
    owning_group_id: str
    issues: list[ApiValidationIssue]


class QcFindingPageResponse(PageResource[QcFindingResource]):
    """Represent one requested page of a report's findings."""

    @classmethod
    def from_service(cls, result: ResultPage[QcFinding]) -> Self:
        """Build a response page from a QC report service result."""
        return cls.from_result_page(
            result,
            lambda finding: QcFindingResource(
                annotation_id=finding.annotation_id,
                annotation_version=finding.annotation_version,
                owning_group_id=finding.owning_group_id,
                issues=[
                    ApiValidationIssue.model_validate(issue) for issue in finding.issues
                ],
            ),
        )


class QcReportListQuery(BaseModel):
    """Filter and page the list of QC reports."""

    model_config = ConfigDict(extra="forbid")

    status: Annotated[
        JobStatus | None, Field(description="Only reports in this state.")
    ] = None
    limit: PageLimit = 50
    offset: PageOffset = 0


class QcReportListItemResource(BaseModel):
    """Identify one QC report in a list, and whether its findings can be read."""

    report_id: UUID
    status: JobStatus
    owning_group_id: str | None = Field(
        description="The group the report covers, or null for all groups."
    )
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    findings_available: bool = Field(
        description=(
            "True when the report succeeded and no later report of its scope has "
            "replaced its findings."
        )
    )


class QcReportPageResponse(PageResource[QcReportListItemResource]):
    """Represent one requested page of QC reports."""

    @classmethod
    def from_service(cls, result: ResultPage[QcReportListing]) -> Self:
        """Build a response page from a QC report service result."""
        return cls.from_result_page(
            result,
            lambda listing: QcReportListItemResource(
                report_id=listing.job.job_id,
                status=listing.job.status,
                owning_group_id=listing.owning_group_id,
                requested_by=listing.job.requested_by,
                created_at=listing.job.created_at,
                started_at=listing.job.started_at,
                completed_at=listing.job.completed_at,
                findings_available=listing.findings_available,
            ),
        )
