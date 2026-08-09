"""Thin, argv-only wrappers around ``git`` and ``gh``.

Every call passes arguments as a list -- no shell string interpolation -- because
PR numbers, paths and branch names are untrusted input from a remote repository.
"""

from __future__ import annotations

import json
import re
import subprocess

US = "\x1f"  # unit separator, used as a field delimiter in git --format


class GitError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, errors="replace"
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"{' '.join(cmd[:3])}... exited {proc.returncode}: "
            f"{proc.stderr.strip()[:400]}"
        )
    return proc.stdout


def git(args: list[str], repo_path: str, check: bool = True) -> str:
    return _run(["git", *args], cwd=repo_path, check=check)


def gh_json(args: list[str], repo_path: str | None = None) -> object | None:
    """Run a gh command expecting JSON on stdout. Returns None on any failure.

    gh is optional everywhere in this engine: network, auth and rate limits can
    all fail, and every gh path has a local-git fallback.
    """
    try:
        out = _run(["gh", *args], cwd=repo_path, check=True)
    except (GitError, FileNotFoundError):
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# ── commit → PR mapping ──────────────────────────────────────────────────────

# Squash-merge subjects end in "(#1234)". Cheapest possible mapping: no network.
_PR_IN_SUBJECT_RE = re.compile(r"\(#(\d+)\)\s*$")

# Process-local memos. A batch run re-blames the same long-lived commits across
# many fix PRs; without these the gh API cost scales with blamed lines.
_PR_CACHE: dict[tuple[str, str], int | None] = {}
_META_CACHE: dict[str, dict] = {}
_SIZE_CACHE: dict[str, tuple[int, int]] = {}


def pr_from_subject(subject: str) -> int | None:
    m = _PR_IN_SUBJECT_RE.search(subject.strip())
    return int(m.group(1)) if m else None


def pr_for_commit(sha: str, subject: str, repo: str, repo_path: str) -> int | None:
    """Map a commit to the PR that merged it: subject regex first, then the API."""
    num = pr_from_subject(subject)
    if num is not None:
        return num
    key = (repo, sha)
    if key in _PR_CACHE:
        return _PR_CACHE[key]
    data = gh_json(
        ["api", f"repos/{repo}/commits/{sha}/pulls", "--jq", "[.[].number]"],
        repo_path,
    )
    result: int | None = None
    if isinstance(data, list) and data:
        try:
            result = int(data[0])
        except (TypeError, ValueError):
            result = None
    _PR_CACHE[key] = result
    return result


# ── commit metadata ──────────────────────────────────────────────────────────


def commit_meta(sha: str, repo_path: str) -> dict:
    if sha in _META_CACHE:
        return _META_CACHE[sha]
    fmt = US.join(["%H", "%an", "%aI", "%s"])
    out = git(["show", "-s", f"--format={fmt}", sha], repo_path).strip()
    parts = out.split(US)
    if len(parts) < 4:
        meta = {"sha": sha, "author": "", "date": "", "subject": ""}
    else:
        meta = {
            "sha": parts[0],
            "author": parts[1],
            "date": parts[2],
            "subject": parts[3],
        }
    _META_CACHE[sha] = meta
    return meta


def commit_size(sha: str, repo_path: str) -> int:
    """Total lines added+deleted by a commit -- used to flag refactor noise."""
    return _numstat(sha, repo_path)[0]


def commit_files(sha: str, repo_path: str) -> int:
    """Files touched by a commit.

    Better than raw line count at separating a bulk port / reformat (hundreds of
    files) from a large but legitimate feature commit (a handful).
    """
    return _numstat(sha, repo_path)[1]


def _numstat(sha: str, repo_path: str) -> tuple[int, int]:
    if sha in _SIZE_CACHE:
        return _SIZE_CACHE[sha]
    # `diff <sha>^ <sha>`, not `show`: `show --numstat` prints nothing for a
    # MERGE commit, so a merge-committed bulk port measured 0 files and evaded
    # the `bulk_port` flag entirely -- the calibration's strongest signal.
    out = git(
        ["diff", "--numstat", "--no-color", f"{sha}^", sha],
        repo_path,
        check=False,
    )
    lines = 0
    files = 0
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 3:
            files += 1
            for col in cols[:2]:
                if col.isdigit():
                    lines += int(col)
    _SIZE_CACHE[sha] = (lines, files)
    return lines, files


# ── fix PR resolution ────────────────────────────────────────────────────────


def merge_commit_for_pr(pr: int, repo: str, repo_path: str, branch: str) -> str | None:
    """Find the commit that landed a PR.

    Local git first (free, offline, no rate limit): squash subjects carry
    ``(#<pr>)``. Falls back to the GitHub API for merge-commit workflows.
    """
    out = git(
        [
            "log",
            branch,
            "--format=" + US.join(["%H", "%s"]),
            # --fixed-strings matters: in a regex "(#123)" is a GROUP, so the
            # parentheses are not matched and the search silently misses every
            # squash subject. Literal substring match, then confirm the token is
            # actually the trailing PR reference via pr_from_subject.
            "--fixed-strings",
            "--grep",
            f"(#{pr})",
            "-40",
        ],
        repo_path,
        check=False,
    )
    for line in out.splitlines():
        parts = line.split(US)
        if len(parts) == 2 and pr_from_subject(parts[1]) == pr:
            return parts[0]

    data = gh_json(
        ["pr", "view", str(pr), "--repo", repo, "--json", "mergeCommit"], repo_path
    )
    if isinstance(data, dict):
        mc = data.get("mergeCommit") or {}
        if isinstance(mc, dict) and mc.get("oid"):
            return str(mc["oid"])
    return None


def pr_meta(pr: int, repo: str, repo_path: str) -> dict:
    """Best-effort PR metadata. Returns {} when gh is unavailable."""
    data = gh_json(
        [
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "number,title,url,mergedAt,labels,author",
        ],
        repo_path,
    )
    if not isinstance(data, dict):
        return {}
    labels = [
        lbl.get("name", "") for lbl in (data.get("labels") or []) if isinstance(lbl, dict)
    ]
    author = data.get("author") or {}
    return {
        "number": data.get("number"),
        "title": data.get("title", ""),
        "url": data.get("url", ""),
        "merged_at": data.get("mergedAt", ""),
        "labels": labels,
        "author": author.get("login", "") if isinstance(author, dict) else "",
    }
