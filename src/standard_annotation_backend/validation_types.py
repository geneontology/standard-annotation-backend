"""Provide shared Pydantic types for common input-validation rules."""

from typing import Annotated

from pydantic import StringConstraints

NonBlankString = Annotated[
    str,
    StringConstraints(pattern=r".*\S.*"),
]

TrimmedNonBlankString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
