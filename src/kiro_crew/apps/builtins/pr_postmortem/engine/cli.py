"""CLI for the attribution engine.
    python3 -m engine.cli attribute --repo owner/name --repo-path /path --pr 1799
    python3 -m engine.cli batch     --repo owner/name --repo-path /path --limit 20
    python3 -m engine.cli discover  --repo-path /path --limit 20
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from .analysis import build_prompt, load_and_validate
from .attribution import attribute
from .bundle import write_bundles
from .discover import discover_fix_prs
from .store import (
    import_jsonl,
    reports_dir,
    retire_analysis,
    touch_scan,
)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default="kirodotdev/KiroCrew", help="owner/name on GitHub")
    p.add_argument("--repo-path", required=True, help="local clone of that repo")
    p.add_argument("--branch", default="origin/main", help="branch fixes land on")
    p.add_argument(
        "--detect-moves",
        action="store_true",
        help="pass -C to git blame (follows moved code; slower)",
    )


def _summary(d: dict) -> str:
    top = (d.get("candidates") or [{}])[0]
    culprit = f"#{top.get('pr')}" if top.get("pr") else (top.get("commits") or [""])[0][:12]
    flags = ",".join(d.get("flags") or []) or "-"
    return (
        f"fix #{d['fix_pr']:<6} -> culprit {culprit:<10} "
        f"conf={d.get('confidence', 0):<5} {d.get('verdict', ''):<9} "
        f"signal={d.get('signal_weight', 0):<7} flags={flags}"
    )


def _analysis_is_about(path: str, culprit_pr: object) -> bool:
    """True when the stored analysis was written about ``culprit_pr``.

    Mirrors `store.load_report`'s coherence rule: equality is the only passing
    case, ``None == None`` included (both sides agreeing there is no culprit PR is
    legitimate for a culprit commit that predates the PR workflow). An unreadable
    or malformed file is NOT fresh -- redoing it is cheap, and trusting it is not.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(stored, dict):
        return False
    return stored.get("culprit_pr") == culprit_pr


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engine.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_att = sub.add_parser("attribute", help="attribute one fix PR")
    _common(p_att)
    p_att.add_argument("--pr", type=int, required=True)
    p_att.add_argument("--out", help="write JSON here instead of stdout")
    p_batch = sub.add_parser("batch", help="attribute the N most recent fix PRs")
    _common(p_batch)
    p_batch.add_argument("--limit", type=int, default=20)
    p_batch.add_argument("--out", required=True, help="JSONL output path")
    p_disc = sub.add_parser("discover", help="list recent merged fix PRs")
    p_disc.add_argument("--repo-path", required=True)
    p_disc.add_argument("--branch", default="origin/main")
    p_disc.add_argument("--limit", type=int, default=20)
    p_bun = sub.add_parser("bundles", help="build evidence bundles from a batch JSONL")
    p_bun.add_argument("--repo", default="kirodotdev/KiroCrew")
    p_bun.add_argument("--repo-path", required=True)
    p_bun.add_argument("--jsonl", required=True, help="attribution batch output")
    p_bun.add_argument("--out-dir", required=True)
    p_bun.add_argument(
        "--only",
        help="comma-separated fix PR numbers; default = every report in the JSONL",
    )
    p_chk = sub.add_parser("check-analysis", help="validate analysis JSON files")
    p_chk.add_argument("--dir", required=True)
    p_pr = sub.add_parser("prompts", help="write per-pair analysis prompt files")
    p_pr.add_argument("--repo", default="kirodotdev/KiroCrew")
    p_pr.add_argument("--bundle-dir", required=True)
    p_pr.add_argument("--out-dir", required=True, help="where analysis-*.json will go")
    p_pr.add_argument("--prompt-dir", required=True)
    p_pr.add_argument(
        "--force",
        action="store_true",
        help="rewrite prompts for pairs that already have an analysis",
    )
    p_imp = sub.add_parser(
        "import-reports", help="split a batch JSONL into per-PR report files"
    )
    p_imp.add_argument("--jsonl", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "import-reports":
        n = import_jsonl(args.jsonl)
        print(f"imported {n} reports into {reports_dir()}")
        return 0
    if args.cmd == "prompts":

        os.makedirs(args.prompt_dir, exist_ok=True)
        os.makedirs(args.out_dir, exist_ok=True)
        written = []
        skipped = 0
        for bpath in sorted(glob.glob(f"{args.bundle_dir}/bundle-*.json")):
            with open(bpath, encoding="utf-8") as fh:
                b = json.load(fh)
            fix_pr = b["fix_pr"]
            out_path = os.path.join(args.out_dir, f"analysis-{fix_pr}.json")
            # Idempotence: a nightly scan must not re-analyse pairs it already
            # explained, or every cycle pays for the whole backlog again. But
            # existence alone is the WRONG test. `load_report` discards an
            # analysis whose `culprit_pr` no longer matches the attribution, so a
            # scan-driven culprit change used to deadlock the pair: discarded at
            # read time (the UI reads "not analysed") yet never regenerated,
            # because the stale file was still sitting there. Skipping now
            # requires the stored analysis to be about THIS culprit.
            # Found by review on PR #2354.
            existing = os.path.exists(out_path)
            fresh = existing and _analysis_is_about(out_path, b.get("culprit_pr"))
            if fresh and not args.force:
                skipped += 1
                continue
            if existing:
                # Retire rather than overwrite: it preserves what the tool
                # believed before, and it is the chokepoint that also clears this
                # pair's positional decisions (an accept on index 2 must not be
                # inherited by whatever new proposal lands at index 2).
                if retire_analysis(fix_pr):
                    reason = "forced" if args.force else "culprit changed"
                    print(f"retired the stored analysis for #{fix_pr} ({reason})")
            text = build_prompt(
                args.repo, bpath, out_path, fix_pr, b.get("culprit_pr")
            )
            ppath = os.path.join(args.prompt_dir, f"prompt-{fix_pr}.txt")
            with open(ppath, "w", encoding="utf-8") as fh:
                fh.write(text)
            written.append(ppath)
            print(ppath)
        print(
            f"{len(written)} prompts written, {skipped} skipped (already analysed)",
            file=sys.stderr,
        )
        return 0
    if args.cmd == "bundles":
        reports = [json.loads(line) for line in open(args.jsonl, encoding="utf-8") if line.strip()]
        if args.only:
            wanted = {int(x) for x in args.only.split(",") if x.strip()}
            reports = [r for r in reports if r.get("fix_pr") in wanted]
        paths = write_bundles(args.repo, args.repo_path, reports, args.out_dir)
        for p in paths:
            print(p)
        print(f"{len(paths)} bundles written", file=sys.stderr)
        return 0
    if args.cmd == "check-analysis":

        files = sorted(glob.glob(f"{args.dir}/analysis-*.json"))
        if not files:
            print(f"no analysis-*.json under {args.dir}")
            return 1
        bad = 0
        for path in files:
            obj, errs = load_and_validate(path)
            name = path.rsplit("/", 1)[-1]
            if errs or obj is None:
                bad += 1
                print(f"INVALID {name}")
                for e in errs or ["unreadable"]:
                    print(f"    - {e}")
            else:
                verdict = obj.get("culprit_link_verdict")
                cls = obj.get("root_cause_class") or "-"
                buckets = ",".join(
                    p.get("bucket", "?") for p in (obj.get("proposals") or [])
                ) or "-"
                inj = " INJECTION-SEEN" if obj.get("prompt_injection_observed") else ""
                print(f"ok      {name:<22} {verdict:<10} {cls:<28} [{buckets}]{inj}")
        print(f"\n{len(files) - bad}/{len(files)} valid")
        return 1 if bad else 0
    if args.cmd == "discover":
        for fx in discover_fix_prs(args.repo_path, args.branch, args.limit):
            print(f"#{fx.pr:<6} {fx.sha[:12]} {fx.date[:10]} {fx.subject}")
        return 0
    if args.cmd == "attribute":
        att = attribute(
            args.repo, args.repo_path, args.pr, args.branch, args.detect_moves
        )
        payload = json.dumps(att.to_dict(), indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(payload + "\n")
            print(_summary(att.to_dict()))
        else:
            print(payload)
        return 0
    # batch
    fixes = discover_fix_prs(args.repo_path, args.branch, args.limit)
    print(f"discovered {len(fixes)} fix PRs", file=sys.stderr)
    verdicts: dict[str, int] = {}
    errors = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for fx in fixes:
            try:
                att = attribute(
                    args.repo, args.repo_path, fx.pr, args.branch, args.detect_moves
                )
                d = att.to_dict()
            except Exception as exc:  # noqa: BLE001 - one bad PR must not kill the run
                d = {
                    "repo": args.repo,
                    "fix_pr": fx.pr,
                    "verdict": "error",
                    "flags": ["engine_error"],
                    "notes": [f"{type(exc).__name__}: {exc}"],
                }
            v = str(d.get("verdict") or "?")
            verdicts[v] = verdicts.get(v, 0) + 1
            errors += 1 if v == "error" else 0
            fh.write(json.dumps(d) + "\n")
            fh.flush()
            print(_summary(d) if d.get("verdict") != "error" else f"fix #{fx.pr} ERROR: {d['notes'][0]}")
    # Record the scan so the UI can show when it last ran. Doing it here rather
    # than leaving it to the cron's agent means the timestamp cannot silently go
    # missing when the workflow is driven some other way.
    rec = touch_scan(
        {
            "repo": args.repo,
            "scanned": len(fixes),
            "verdicts": verdicts,
            "errors": errors,
        }
    )
    print(f"last_scan recorded at {rec['at']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
