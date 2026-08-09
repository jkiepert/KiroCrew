"""Attribute a fix PR back to the PR that introduced the bug.

Mechanically, not by guesswork:

1. Take the fix's landed commit ``F``. Its first parent ``F^`` is the exact tree
   the bug lived in.
2. Diff ``F^..F`` at zero context to get the *pre-image* lines the fix deleted or
   rewrote (see :mod:`engine.diffparse`).
3. ``git blame`` those line ranges at ``F^``. Each line names the commit that
   last wrote it.
4. Map those commits to their PRs and rank by weighted attributed lines.

The output carries its own evidence and caveats: attribution is a heuristic, and
a report that cannot show which lines produced its verdict is not reviewable.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Iterable

from . import vcs
from .diffparse import W_ANCHOR, W_MODIFIED, FileChange, parse_pre_image

MAX_EVIDENCE_ROWS = 40
# A culprit touching this many files is a bulk port, mass reformat or repo
# import: blame lands on it because it MOVED the lines into this history, so the
# author who actually wrote the bug is outside this repo and unrecoverable from
# it. Measured on kirodotdev/KiroCrew: every unrecoverable culprit touched
# 231-774 files, every correct one <= 76. Raw line count cannot separate them --
# a legitimate feature PR reaches 1.9k lines across 15 files.
BULK_PORT_FILES = 100
# Large but still plausibly the author. Informational only.
LARGE_COMMIT_FILES = 40
LOW_SIGNAL_WEIGHT = 4.0
STRONG_SHARE = 0.70
WEAK_SHARE = 0.40

# Flags that make a verdict untrustworthy no matter how high its share is.
BLOCKING_FLAGS = frozenset({"bulk_port", "diffuse", "no_source_signal"})


def compute_verdict(share: float, flags: set[str]) -> str:
    """Map a top-candidate share plus caveat flags onto a verdict.

    ``low_signal`` is deliberately NOT blocking: a one-line fix whose single
    blamed line resolves to one commit is a good attribution, not a weak one.
    Thin evidence is reported as a flag so a reader can weigh it.
    """
    if BLOCKING_FLAGS & flags:
        return "weak"
    if share >= STRONG_SHARE:
        return "strong"
    return "moderate"


_BLAME_HEADER_RE = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")


@dataclass
class EvidenceRow:
    """One blamed line range -- the reviewable unit of an attribution."""

    file: str
    kind: str  # source | test | i18n
    pre_image_lines: str  # e.g. "412-418"
    weight: float
    culprit_sha: str
    culprit_pr: int | None
    author: str
    date: str
    subject: str


@dataclass
class Candidate:
    """A PR (or bare commit) accused of introducing the bug."""

    pr: int | None
    weight: float
    share: float
    commits: list[str] = field(default_factory=list)
    subject: str = ""
    author: str = ""
    date: str = ""
    largest_commit_lines: int = 0
    largest_commit_files: int = 0
    source_weight: float = 0.0  # weight from non-test, non-i18n files only


@dataclass
class Attribution:
    repo: str
    fix_pr: int
    fix_commit: str
    pre_image_rev: str
    fix_title: str = ""
    fix_url: str = ""
    fix_merged_at: str = ""
    fix_labels: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    evidence: list[EvidenceRow] = field(default_factory=list)
    signal_weight: float = 0.0
    confidence: float = 0.0
    verdict: str = "none"  # strong | moderate | weak | none
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    files_considered: int = 0
    files_skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["candidates"] = [asdict(c) for c in self.candidates]
        d["evidence"] = [asdict(e) for e in self.evidence]
        return d

    @property
    def top(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def _blame_range(
    repo_path: str, rev: str, path: str, start: int, end: int, detect_moves: bool
) -> dict[int, str]:
    """Blame one line range. Returns ``{line_no: sha}``; empty on failure."""
    args = ["blame", "--line-porcelain", "-w", "-L", f"{start},{end}"]
    if detect_moves:
        # -C follows code moved between files in the same commit, which keeps a
        # pure file-split refactor from swallowing the real culprit.
        args.append("-C")
    args += [rev, "--", path]
    out = vcs.git(args, repo_path, check=False)
    result: dict[int, str] = {}
    for line in out.splitlines():
        m = _BLAME_HEADER_RE.match(line)
        if m:
            # Key by the FINAL line number (group 3), not the original (group 2).
            #
            # The porcelain header is `<sha> <orig_line> <final_line> [<n>]`. The
            # range being blamed is expressed in THIS revision's coordinates, so
            # the final line is the unique key. Keying by the original line number
            # collides whenever two lines from different commits happen to share a
            # position in their own source file -- one silently overwrites the
            # other and the per-commit line counts that drive the weighting come
            # out short. Found by review on PR #2354.
            result[int(m.group(3))] = m.group(1)
    return result


def _range_specs(fc: FileChange) -> list[tuple[int, int, float]]:
    """Yield ``(start, end, per_line_weight)`` for a file's pre-image footprint."""
    specs = [(s, s + c - 1, W_MODIFIED) for s, c in fc.ranges if c > 0]
    specs += [(a, a, W_ANCHOR) for a in fc.anchors]
    return specs


def _contiguous_runs(line_nos: Iterable[int]) -> list[list[int]]:
    """Split line numbers into ascending runs of consecutive integers.

    ``[10, 11, 20]`` -> ``[[10, 11], [20]]``. Used so an evidence span never
    covers a line that belongs to a different commit.
    """
    runs: list[list[int]] = []
    for n in sorted(set(line_nos)):
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    return runs


def attribute(
    repo: str,
    repo_path: str,
    fix_pr: int,
    branch: str = "origin/main",
    detect_moves: bool = False,
) -> Attribution:
    """Resolve the PR that most likely introduced the bug fixed by ``fix_pr``."""
    fix_commit = vcs.merge_commit_for_pr(fix_pr, repo, repo_path, branch)
    if not fix_commit:
        return Attribution(
            repo=repo,
            fix_pr=fix_pr,
            fix_commit="",
            pre_image_rev="",
            verdict="none",
            flags=["fix_commit_not_found"],
            notes=[
                f"No commit on {branch} carries '(#{fix_pr})' and gh could not "
                "resolve a merge commit. Is the PR merged, and is the branch fetched?"
            ],
        )

    pre_image_rev = f"{fix_commit}^"
    meta = vcs.pr_meta(fix_pr, repo, repo_path)
    att = Attribution(
        repo=repo,
        fix_pr=fix_pr,
        fix_commit=fix_commit,
        pre_image_rev=pre_image_rev,
        fix_title=meta.get("title", "") or vcs.commit_meta(fix_commit, repo_path)["subject"],
        fix_url=meta.get("url", "") or f"https://github.com/{repo}/pull/{fix_pr}",
        fix_merged_at=meta.get("merged_at", ""),
        fix_labels=meta.get("labels", []),
    )

    # `diff <fix>^ <fix>`, NOT `show <fix>`.
    #
    # `git show` prints no diff at all for a MERGE commit, so in any repository
    # that merges rather than squashes, every fix PR yielded an empty pre-image
    # and the verdict came back `no_pre_image_signal` -- the tool silently found
    # nothing. The documented design was always "diff F^..F"; the implementation
    # had drifted to `show`. For a merge commit `<fix>^` is the first parent, so
    # this is exactly the set of changes the merge brought in. Found by review on
    # PR #2354.
    diff = vcs.git(
        [
            "diff",
            "--unified=0",
            "-M",
            "--no-color",
            f"{fix_commit}^",
            fix_commit,
        ],
        repo_path,
        check=False,
    )
    files = parse_pre_image(diff)
    att.files_considered = sum(1 for f in files if not f.excluded)
    for f in files:
        if f.excluded:
            reason = "new-file" if f.is_new_file else f.kind
            att.files_skipped.append(f"{f.new_path} ({reason})")

    # ── blame every pre-image range ──
    weight_by_sha: dict[str, float] = defaultdict(float)
    source_weight_by_sha: dict[str, float] = defaultdict(float)
    rows: list[tuple[float, str, str, str, str]] = []  # (weight, file, kind, lines, sha)

    for fc in files:
        if fc.excluded:
            continue
        for start, end, per_line in _range_specs(fc):
            blamed = _blame_range(
                repo_path, pre_image_rev, fc.path, start, end, detect_moves
            )
            if not blamed:
                att.notes.append(
                    f"blame failed for {fc.path}:{start}-{end} at {pre_image_rev[:12]} "
                    "(renamed or absent at that revision)"
                )
                continue
            # Group contiguous lines sharing a culprit into one evidence row.
            per_sha: dict[str, list[int]] = defaultdict(list)
            for line_no, sha in blamed.items():
                per_sha[sha].append(line_no)
            for sha, line_nos in per_sha.items():
                w = per_line * len(line_nos) * fc.multiplier
                weight_by_sha[sha] += w
                if fc.kind == "source":
                    source_weight_by_sha[sha] += w
                # One row per CONTIGUOUS run -- not one row spanning
                # min..max. With interleaved authorship (this commit owns 10-11
                # and 20 while another owns 12-19) the collapsed form read
                # "10-20", claiming lines the culprit never wrote. The evidence a
                # human reads to judge a verdict IS this app's product, so a span
                # that overstates it is a correctness bug even though the maths
                # was fine: weight counted `len(line_nos)` all along, and the
                # per-run weights sum back to exactly the same total.
                # Found by review on PR #2354.
                for run in _contiguous_runs(line_nos):
                    rows.append((
                        per_line * len(run) * fc.multiplier,
                        fc.path,
                        fc.kind,
                        f"{run[0]}" if len(run) == 1 else f"{run[0]}-{run[-1]}",
                        sha,
                    ))

    att.signal_weight = round(sum(weight_by_sha.values()), 2)
    if not weight_by_sha:
        att.verdict = "none"
        att.flags.append("no_pre_image_signal")
        att.notes.append(
            "The fix only added lines (or touched only excluded files), so no "
            "pre-image line points at an introducing commit."
        )
        return att

    # ── roll commits up into PR-level candidates ──
    sha_meta = {sha: vcs.commit_meta(sha, repo_path) for sha in weight_by_sha}
    sha_pr: dict[str, int | None] = {}
    for sha, m in sha_meta.items():
        pr = vcs.pr_for_commit(sha, m["subject"], repo, repo_path)
        # A self-attribution means blame landed on the fix's own commit; drop it.
        sha_pr[sha] = None if pr == fix_pr else pr

    grouped: dict[object, Candidate] = {}
    for sha, w in weight_by_sha.items():
        pr = sha_pr[sha]
        key = ("pr", pr) if pr is not None else ("sha", sha)
        cand = grouped.get(key)
        if cand is None:
            m = sha_meta[sha]
            cand = Candidate(
                pr=pr,
                weight=0.0,
                share=0.0,
                subject=m["subject"],
                author=m["author"],
                date=m["date"],
            )
            grouped[key] = cand
        cand.weight += w
        cand.source_weight += source_weight_by_sha.get(sha, 0.0)
        cand.commits.append(sha)
        cand.largest_commit_lines = max(
            cand.largest_commit_lines, vcs.commit_size(sha, repo_path)
        )
        cand.largest_commit_files = max(
            cand.largest_commit_files, vcs.commit_files(sha, repo_path)
        )

    total = sum(c.weight for c in grouped.values())
    candidates = sorted(grouped.values(), key=lambda c: c.weight, reverse=True)
    for c in candidates:
        c.weight = round(c.weight, 2)
        c.source_weight = round(c.source_weight, 2)
        c.share = round(c.weight / total, 3) if total else 0.0
    att.candidates = candidates[:5]

    # ── evidence table ──
    rows.sort(key=lambda r: r[0], reverse=True)
    for w, path, kind, span, sha in rows[:MAX_EVIDENCE_ROWS]:
        m = sha_meta[sha]
        att.evidence.append(
            EvidenceRow(
                file=path,
                kind=kind,
                pre_image_lines=span,
                weight=round(w, 2),
                culprit_sha=sha[:12],
                culprit_pr=sha_pr[sha],
                author=m["author"],
                date=m["date"],
                subject=m["subject"],
            )
        )
    if len(rows) > MAX_EVIDENCE_ROWS:
        att.notes.append(
            f"evidence truncated to {MAX_EVIDENCE_ROWS} of {len(rows)} blamed ranges"
        )

    # ── confidence and honest caveats ──
    top = candidates[0]
    att.confidence = top.share
    if att.signal_weight < LOW_SIGNAL_WEIGHT:
        att.flags.append("low_signal")
    if top.share < WEAK_SHARE:
        att.flags.append("diffuse")
    if top.largest_commit_files >= BULK_PORT_FILES:
        att.flags.append("bulk_port")
    elif top.largest_commit_files >= LARGE_COMMIT_FILES:
        att.flags.append("large_commit")
    if top.source_weight == 0.0:
        att.flags.append("no_source_signal")
    if top.pr is None:
        att.flags.append("unmapped_commit")

    att.verdict = compute_verdict(top.share, set(att.flags))
    return att
