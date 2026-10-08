"""Verify the checks shared by the GPI and GPAD readers."""

import pytest

from standard_annotation_backend.exchange_files import (
    NulHeaderLineError,
    nul_data_lines,
)


def test_nul_data_lines_returns_one_based_lines_containing_nul() -> None:
    """Data lines with a NUL character are reported by one-based line number."""
    text = "!gpi-version: 2.0\nfirst\nsec\x00ond\nthird\nfo\x00urth"

    assert nul_data_lines(text) == frozenset({3, 5})


def test_nul_in_header_line_raises_with_its_line_number() -> None:
    """A NUL character in a header line rejects the document at that line."""
    text = "!gpi-version: 2.0\nda\x00ta\n!custom: va\x00lue\n"

    with pytest.raises(NulHeaderLineError) as caught:
        nul_data_lines(text)

    assert caught.value.line_number == 3
    assert caught.value.message == "line 3 contains a NUL character"
