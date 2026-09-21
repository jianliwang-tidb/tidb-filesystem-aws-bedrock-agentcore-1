from __future__ import annotations

import re
from pathlib import Path

_TEST_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)


def validate_test_run_id(test_run_id: object) -> str:
    """Validate and return a test-run identifier with no path components."""
    if not isinstance(test_run_id, str) or not _TEST_RUN_ID_PATTERN.fullmatch(test_run_id):
        raise ValueError(
            "test_run_id must be 1-128 ASCII letters, digits, dots, underscores, or hyphens "
            "and must start with a letter or digit"
        )
    return test_run_id


def safe_output_directory(output_root: str | Path, test_run_id: object) -> Path:
    """Return a test-run directory that is guaranteed to stay under output_root."""
    validated_test_run_id = validate_test_run_id(test_run_id)
    resolved_root = Path(output_root).expanduser().resolve()
    resolved_output = (resolved_root / validated_test_run_id).resolve()
    try:
        resolved_output.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("test_run_id resolves outside the output directory") from exc
    return resolved_output
