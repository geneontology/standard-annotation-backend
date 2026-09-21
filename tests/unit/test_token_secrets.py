"""Verify opaque credentials and their safe lookup digests."""

import base64
import hashlib

from standard_annotation_backend.auth.secrets import (
    digest_secret,
    generate_secret,
    secret_matches,
)


def test_generated_secrets_are_distinct_prefixed_256_bit_values() -> None:
    """Generated credentials carry 32 random bytes and a separate SHA-256 digest."""
    generated = [generate_secret("sab_") for _ in range(100)]
    assert len({raw for raw, _digest in generated}) == 100
    for raw, digest in generated:
        assert raw.startswith("sab_")
        assert len(base64.urlsafe_b64decode(raw.removeprefix("sab_") + "=")) == 32
        assert digest == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in digest


def test_digest_lookup_and_comparison_handle_untrusted_text() -> None:
    """Digesting is deterministic and credential comparison safely handles Unicode."""
    digest = hashlib.sha256(b"sab_example").hexdigest()
    assert digest_secret("sab_example") == digest
    assert secret_matches("sab_example", digest)
    assert not secret_matches("sab_other", digest)
    assert not secret_matches("sab_\N{SNOWMAN}", digest)
