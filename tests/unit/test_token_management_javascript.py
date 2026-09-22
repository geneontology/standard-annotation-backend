"""Execute deterministic token-management browser behavior with Node.js."""

import subprocess


def test_token_management_javascript_behavior() -> None:
    """Deterministic browser rules execute outside source assertions."""
    result = subprocess.run(
        ["node", "--test", "tests/javascript/token_management.test.cjs"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
