"""Extract *pre-image* line ranges from a unified diff.

The pre-image is the set of lines a fix DELETED or MODIFIED -- i.e. the lines
that carried the bug. Those are the only lines worth blaming; the lines the fix
ADDED never existed before it, so they carry no attribution signal.

The parser is deliberately dependency-free and works on the output of::

    git show --format= --unified=0 -M <sha>

``--unified=0`` matters: with context lines, hunk headers cover untouched
neighbours and blame would attribute innocent commits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# @@ -old_start[,old_count] +new_start[,new_count] @@ [heading]
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_OLD_PATH_RE = re.compile(r"^--- (?:a/)?(.*)$")
_NEW_PATH_RE = re.compile(r"^\+\+\+ (?:b/)?(.*)$")

# Weight applied to a line of attribution signal.
W_MODIFIED = 1.0  # a line the fix deleted or rewrote -- the strongest signal
W_ANCHOR = 0.4  # the line a pure insertion was placed after -- weaker

# Path-class multipliers. A bug fixed in product code points at the commit that
# wrote that code; churn in tests or translations points at it far more weakly.
M_TEST = 0.25
M_I18N = 0.25
M_DOCS = 0.25

_TEST_RE = re.compile(
    r"(^|/)tests?/|(^|/)__tests__/|(^|/)test_[^/]+\.py$|[._-]test\.[jt]sx?$"
    r"|\.test\.[jt]sx?$|[._-]spec\.[jt]sx?$|_test\.go$"
)
_I18N_RE = re.compile(r"(^|/)(locales?|i18n|lang)/|\.(po|pot)$")
# Prose is not the bug. A fix usually updates the doc it invalidated, and blaming
# that prose line points at whoever last edited the sentence -- not the defect.
_DOCS_RE = re.compile(r"\.(md|mdx|rst|txt)$|(^|/)docs?/|(^|/)CHANGELOG")
# Machine-owned files: churn here is noise, never a root cause.
_GENERATED_RE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock"
    r"|go\.sum|uv\.lock)$"
    r"|\.min\.(js|css)$|(^|/)dist/|(^|/)build/|(^|/)node_modules/|(^|/)vendor/"
    r"|\.snap$|(^|/)__snapshots__/"
)


def classify(path: str) -> tuple[float, str]:
    """Return ``(multiplier, label)`` for a path. Multiplier 0.0 == excluded."""
    if _GENERATED_RE.search(path):
        return 0.0, "generated"
    if _TEST_RE.search(path):
        return M_TEST, "test"
    if _I18N_RE.search(path):
        return M_I18N, "i18n"
    if _DOCS_RE.search(path):
        return M_DOCS, "docs"
    return 1.0, "source"


@dataclass
class FileChange:
    """Pre-image footprint of one file in a fix."""

    path: str  # path as it existed BEFORE the fix (blame target)
    new_path: str
    multiplier: float
    kind: str  # source | test | i18n | generated
    is_new_file: bool = False
    is_deleted_file: bool = False
    # (start, count) line ranges the fix deleted/modified, in pre-image numbering
    ranges: list[tuple[int, int]] = field(default_factory=list)
    # single pre-image lines that a pure insertion was anchored after
    anchors: list[int] = field(default_factory=list)

    @property
    def excluded(self) -> bool:
        return self.multiplier == 0.0 or self.is_new_file

    def signal_weight(self) -> float:
        modified = sum(count for _, count in self.ranges) * W_MODIFIED
        anchored = len(self.anchors) * W_ANCHOR
        return (modified + anchored) * self.multiplier


def parse_pre_image(diff_text: str) -> list[FileChange]:
    """Parse a unified diff into per-file pre-image ranges.

    New files are recorded but flagged ``is_new_file`` (nothing to blame).
    Renames are handled by blaming the OLD path, which is what existed at the
    pre-image revision.
    """
    files: list[FileChange] = []
    cur: FileChange | None = None
    old_path: str | None = None
    # Creation and deletion are read from git's EXTENDED HEADER (`new file mode`
    # / `deleted file mode`) rather than from the `/dev/null` path sentinel.
    #
    # Two reasons, in order of importance. The header is the authoritative signal:
    # git emits it for exactly these two cases, whereas the sentinel is a
    # side-effect of how the `---`/`+++` lines are rendered. And it keeps an
    # absolute POSIX path literal out of shipped source -- `/dev/null` is a git
    # protocol token that appears on Windows too, so `os.devnull` would be WRONG
    # here, and the cross-platform gate cannot tell the difference.
    saw_new_file = False
    saw_deleted_file = False

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            cur = None
            old_path = None
            saw_new_file = False
            saw_deleted_file = False
            continue

        if line.startswith("new file mode "):
            saw_new_file = True
            continue
        if line.startswith("deleted file mode "):
            saw_deleted_file = True
            continue

        # Order matters: '--- ' and '+++ ' must be tested before the generic
        # '-'/'+' content lines, and '+++' before '++'-prefixed content.
        m = _OLD_PATH_RE.match(line)
        if m and not line.startswith("---- "):
            old_path = None if saw_new_file else m.group(1)
            continue

        m = _NEW_PATH_RE.match(line)
        if m:
            new_path = m.group(1)
            is_new = saw_new_file or old_path is None
            is_deleted = saw_deleted_file
            blame_path = old_path or new_path
            mult, kind = classify(blame_path)
            cur = FileChange(
                path=blame_path,
                new_path=new_path,
                multiplier=mult,
                kind=kind,
                is_new_file=is_new,
                is_deleted_file=is_deleted,
            )
            files.append(cur)
            continue

        m = _HUNK_RE.match(line)
        if m and cur is not None:
            old_start = int(m.group(1))
            old_count = 1 if m.group(2) is None else int(m.group(2))
            if old_count > 0:
                cur.ranges.append((old_start, old_count))
            elif old_start >= 1:
                # Pure insertion: `-N,0` means "inserted after pre-image line N".
                cur.anchors.append(old_start)

    return files


def total_signal(files: list[FileChange]) -> float:
    return sum(f.signal_weight() for f in files if not f.excluded)
