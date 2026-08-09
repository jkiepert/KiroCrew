"""Validation harness: measure attribution quality over a batch of fix PRs.

Reads a JSONL produced by ``engine.cli batch`` and reports:

* verdict / flag distribution
* a compact per-pair table (fix title vs named culprit) for human judgement
* mechanical failure-mode classification, so "the engine is wrong" is separated
  from "this repo cannot answer the question"

Also provides ``blame-strength``, which re-blames one report's evidence at three
move-detection levels. Without that check, an unchanged culprit under ``-C`` is
ambiguous: it can mean "no moved code" or "the knob was too weak to matter".

    python3 -m tools.validate summary     --jsonl /tmp/att.jsonl
    python3 -m tools.validate table       --jsonl /tmp/att.jsonl
    python3 -m tools.validate compare     --jsonl /tmp/a.jsonl --other /tmp/b.jsonl
    python3 -m tools.validate blame-strength --jsonl /tmp/att.jsonl --pr 2195 \
        --repo-path /path/to/clone
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter

# A culprit commit touching more files than this is a bulk port / sync / mass
# reformat. Blame lands on it because it moved the lines into this repo, so the
# real author is outside this history and cannot be recovered from it.
BULK_PORT_FILES = 100
# Above this, a commit is a large feature -- big, but still plausibly the author.
LARGE_FEATURE_FILES = 40


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def top_of(row: dict) -> dict:
    cands = row.get("candidates") or []
    return cands[0] if cands else {}


def culprit_label(row: dict) -> str:
    top = top_of(row)
    if top.get("pr"):
        return f"#{top['pr']}"
    commits = top.get("commits") or []
    return commits[0][:12] if commits else "-"


def failure_mode(row: dict) -> str:
    """Classify why a pair is not a clean, actionable attribution."""
    top = top_of(row)
    if not top:
        return "no_candidate"
    files = top.get("largest_commit_files") or 0
    flags = set(row.get("flags") or [])
    if files >= BULK_PORT_FILES or "bulk_port" in flags:
        return "unknowable_upstream"  # bulk port: author is outside this repo
    if "diffuse" in flags:
        return "diffuse_multi_origin"  # the fix spans code from several PRs
    if "no_source_signal" in flags:
        return "non_source_only"  # only tests/docs/i18n lines carried signal
    if "unmapped_commit" in flags:
        return "pre_pr_history"  # commit predates PR-based workflow
    if "low_signal" in flags:
        return "thin_evidence"
    return "clean"


def cmd_summary(rows: list[dict]) -> None:
    print(f"reports: {len(rows)}")
    print("\nverdicts:")
    for verdict, n in Counter(r.get("verdict", "?") for r in rows).most_common():
        print(f"  {verdict:<10} {n:>3}  ({n / len(rows):.0%})")
    print("\nflags (a report can carry several):")
    flat = Counter(f for r in rows for f in (r.get("flags") or []))
    for flag, n in flat.most_common():
        print(f"  {flag:<20} {n:>3}  ({n / len(rows):.0%})")
    print("\nfailure modes:")
    for mode, n in Counter(failure_mode(r) for r in rows).most_common():
        print(f"  {mode:<22} {n:>3}  ({n / len(rows):.0%})")
    named = sum(1 for r in rows if top_of(r))
    mapped = sum(1 for r in rows if (top_of(r).get("pr")))
    print(f"\nnamed a culprit:        {named}/{len(rows)}")
    print(f"named a culprit *PR*:   {mapped}/{len(rows)}")


def cmd_table(rows: list[dict]) -> None:
    hdr = (
        f"{'fix':>6} {'culprit':>9} {'conf':>5} {'sig':>6} {'files':>5} "
        f"{'lines':>6} {'mode':<22} fix title / culprit subject"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        top = top_of(r)
        print(
            f"{r['fix_pr']:>6} {culprit_label(r):>9} "
            f"{r.get('confidence', 0):>5} {r.get('signal_weight', 0):>6} "
            f"{top.get('largest_commit_files', 0):>5} "
            f"{top.get('largest_commit_lines', 0):>6} "
            f"{failure_mode(r):<22} {(r.get('fix_title') or '')[:58]}"
        )
        print(f"{'':>58}   ^-- culprit: {(top.get('subject') or '')[:70]}")


def cmd_compare(rows: list[dict], other: list[dict]) -> None:
    by_pr = {r["fix_pr"]: r for r in other}
    changed = 0
    for r in rows:
        o = by_pr.get(r["fix_pr"])
        if not o:
            continue
        a, b = culprit_label(r), culprit_label(o)
        if a != b:
            changed += 1
            print(f"fix #{r['fix_pr']}: {a} -> {b}  (culprit CHANGED)")
    print(f"\n{changed}/{len(rows)} culprits changed between the two runs")


def cmd_blame_strength(rows: list[dict], pr: int, repo_path: str) -> None:
    match = [r for r in rows if r.get("fix_pr") == pr]
    if not match:
        print(f"no report for fix #{pr}")
        return
    row = match[0]
    rev = row["pre_image_rev"]
    levels = {
        "plain": ["-w"],
        "-C": ["-w", "-C"],
        "-C -C -C": ["-w", "-C", "-C", "-C"],
    }
    print(f"fix #{pr} at {rev[:14]}")
    for ev in row.get("evidence", [])[:5]:
        span = ev["pre_image_lines"]
        lo = span.split("-")[0]
        hi = span.split("-")[-1]
        print(f"  {ev['file']}:{span}")
        for name, flags in levels.items():
            cmd = [
                "git", "blame", "--line-porcelain", *flags,
                "-L", f"{lo},{hi}", rev, "--", ev["file"],
            ]
            proc = subprocess.run(
                cmd, cwd=repo_path, capture_output=True, text=True
            )
            shas = sorted(
                {
                    parts[0][:10]
                    for line in proc.stdout.splitlines()
                    if len(parts := line.split()) > 2 and len(parts[0]) == 40
                }
            )
            print(f"     {name:<10} -> {shas or 'blame failed'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools.validate")
    ap.add_argument("cmd", choices=["summary", "table", "compare", "blame-strength"])
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--other", help="second JSONL for compare")
    ap.add_argument("--pr", type=int, help="fix PR for blame-strength")
    ap.add_argument("--repo-path", help="local clone, for blame-strength")
    args = ap.parse_args(argv)

    rows = load(args.jsonl)
    if args.cmd == "summary":
        cmd_summary(rows)
    elif args.cmd == "table":
        cmd_table(rows)
    elif args.cmd == "compare":
        if not args.other:
            ap.error("compare needs --other")
        cmd_compare(rows, load(args.other))
    else:
        if not (args.pr and args.repo_path):
            ap.error("blame-strength needs --pr and --repo-path")
        cmd_blame_strength(rows, args.pr, args.repo_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
