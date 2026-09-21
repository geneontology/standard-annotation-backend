"""Generate opaque credentials and compare their SHA-256 lookup digests."""

import hashlib
import secrets

API_TOKEN_PREFIX = "sab_token_"
TOKEN_MANAGEMENT_SESSION_PREFIX = "sab_session_"


def digest_secret(raw: str) -> str:
    """Hash an opaque credential for lookup without retaining its raw value.

    Args:
        raw: Credential to hash.

    Returns:
        Lowercase hexadecimal SHA-256 digest.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_secret(prefix: str) -> tuple[str, str]:
    """Generate a prefixed 256-bit credential and its storage-safe digest.

    Args:
        prefix: Text identifying the credential type.

    Returns:
        The raw credential followed by its lowercase hexadecimal SHA-256 digest.
    """
    raw = prefix + secrets.token_urlsafe(32)
    return raw, digest_secret(raw)


def secret_matches(raw: str, digest: str) -> bool:
    """Compare a raw credential with a trusted digest in constant time.

    Args:
        raw: Untrusted credential to hash and compare.
        digest: Trusted lowercase hexadecimal SHA-256 digest.

    Returns:
        Whether the credential has the trusted digest.
    """
    return secrets.compare_digest(digest_secret(raw), digest)
