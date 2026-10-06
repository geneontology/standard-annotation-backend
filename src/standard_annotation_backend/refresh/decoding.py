"""Decode fetched source bytes into text for the GPI and GPAD parsers."""

import gzip
import zlib

from standard_annotation_backend.domain.refresh import RefreshFailureCode
from standard_annotation_backend.refresh.fetchers import SourceError

_GZIP_SIGNATURE = b"\x1f\x8b"


def decode_source_text(content: bytes) -> str:
    """Return the text of fetched source bytes, expanding gzip when present.

    Gzip is recognized by its two-byte signature rather than by the URL or a
    response header, so a source can switch between compressed and plain files
    without a configuration change. Decoding is strict: any byte sequence that
    is not valid UTF-8 is an error rather than being replaced.

    Raises:
        SourceError: With code `invalid_gzip` if the gzip data is corrupt, or
            `invalid_utf8` if the text is not valid UTF-8.
    """
    if content.startswith(_GZIP_SIGNATURE):
        try:
            content = gzip.decompress(content)
        except (OSError, EOFError, zlib.error):
            raise SourceError(RefreshFailureCode.INVALID_GZIP) from None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceError(RefreshFailureCode.INVALID_UTF8) from None
