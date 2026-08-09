"""Credential redactor for this app's model-authored data.

Everything this app serves is derived from two untrusted sources: pull-request
prose written by anyone who can open a PR, and an analyst model's free text about
it. Both are persisted verbatim under the app's data directory and both reach a
browser surface -- so an AKIA key or a `ghp_…` token pasted into a PR body would
otherwise be rendered to the dashboard unscrubbed.

SCOPE, measured rather than assumed (see tests/test_review_regressions.py):
``redact_credentials`` removes credential SHAPES. ``redact_exfiltration_urls`` is
applied as well for parity with the other app sinks, but it does NOT filter
ordinary third-party links -- a `https://webhook.site/...` URL, and even
`https://user:pass@host`, pass through. This is a credential scrub, not a link
sanitiser, and the difference matters when reading a report.

Mirrors ``apps/builtins/mochi/redact.py``, which exists for exactly this shape of
problem. Kept as a leaf module (depending only on ``kiro_crew.security``) so the
HTTP routes can import it at module scope without a circular import.

Found by review on PR #2354: the report and backlog responses returned stored
analysis text straight to the client with no redaction pass.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def redact_tree(value: Any) -> Any:
    """Recursively redact credentials + exfiltration URLs in a JSON-like value.

    Strings are scrubbed; lists and dicts are walked; other scalars pass through.
    Applied at the HTTP response chokepoint rather than at write time, so a report
    written by an older version is still scrubbed when it is served.
    """
    if isinstance(value, str):
        redacted, _ = redact_credentials(value)
        redacted, _ = redact_exfiltration_urls(redacted)
        return redacted
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_tree(item) for key, item in value.items()}
    return value
