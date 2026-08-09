"""Find merged fix PRs worth running a postmortem on."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import vcs

# Conventional-commit fix subjects. Deliberately narrow: `feat:` PRs that happen
# to repair something are not reliably identifiable, and a false positive costs a
# whole wasted analysis.
_FIX_SUBJECT_RE = re.compile(r"^(fix|bugfix|hotfix|revert)(\([^)]*\))?!?:", re.I)


@dataclass
class FixPR:
    pr: int
    sha: str
    subject: str
    date: str


def discover_fix_prs(
    repo_path: str, branch: str = "origin/main", limit: int = 20, scan: int = 400
) -> list[FixPR]:
    """Return the most recent merged fix PRs on ``branch``, newest first.

    ``scan`` bounds how far back through history we look for ``limit`` matches.
    """
    fmt = vcs.US.join(["%H", "%aI", "%s"])
    out = vcs.git(
        ["log", branch, f"--format={fmt}", f"-{scan}"], repo_path, check=False
    )
    found: list[FixPR] = []
    for line in out.splitlines():
        parts = line.split(vcs.US)
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        if not _FIX_SUBJECT_RE.match(subject):
            continue
        pr = vcs.pr_from_subject(subject)
        if pr is None:
            continue
        found.append(FixPR(pr=pr, sha=sha, subject=subject, date=date))
        if len(found) >= limit:
            break
    return found
