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

ExactNonBlankString = Annotated[
    str,
    StringConstraints(pattern=r"^\S(?:.*\S)?$"),
]
"""A non-blank string with no leading or trailing whitespace, rejected rather than
stripped, for values such as group keys that are stored and matched exactly."""
