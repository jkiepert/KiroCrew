# Attribution validation

Measured on the 20 most recent merged fix PRs of `kirodotdev/KiroCrew`
(2026-08-08). Every pair was hand-judged against the fix title, the named
culprit's subject, and the blamed evidence rows.

## Ground truth

| class | n | fix PRs |
|---|---|---|
| correct culprit | 12 | 1799, 1811, 1842, 2187, 2108, 2206, 2214, 2223, 2194, 2195, 2196, 2184 |
| plausible (same subsystem, diffuse origin) | 2 | 1899, 2191 |
| unknowable — bulk port, author outside this repo | 5 | 1802, 1863, 1895, 1900, 2179 |
| wrong | 1 | 1901 |

20/20 pairs named a culprit; 15/20 named a culprit **PR** (the rest predate the
PR-based workflow). Excluding the 5 unanswerable pairs, 12–14 of 15 are correct.

## Calibrated result

| verdict | n | incorrect |
|---|---|---|
| strong | 5 | 0 |
| moderate | 5 | 0 |
| weak | 10 | — (holds all 5 unknowable + the 1 wrong) |

**100% precision on actionable verdicts, 71% recall** (10 surfaced of 14
correct-or-plausible). The 4 suppressed hits are a deliberate cost: 1842 and
1901 are indistinguishable by file count, and 2206/2184 are test-only fixes with
no product-code signal to check.

## What the calibration changed

`refactor_suspect` keyed on total lines changed (>= 800) and fired on 15/20,
collapsing everything to `weak` — including 12 correct attributions.

Line count cannot separate a bulk port from a large feature PR: a legitimate
feature reaches 1.9k lines across 15 files. **File count can.** Every
unrecoverable culprit here touched 231–774 files; every correct one touched
<= 76. So `bulk_port` now keys on >= 100 files, with `large_commit` (>= 40) kept
informational.

`low_signal` also stopped blocking `strong`: a one-line fix whose single blamed
line resolves to one commit is a good attribution, not a weak one. It survives as
a flag so a reader can weigh the thinness.

`diffuse` moved from `< 0.50` to `< 0.40` share, which surfaced three correct
culprits (2223, 2196, 2191) that the wider band had buried.

## Move detection buys nothing here

`git blame -C` changed **0 of 20** culprits, and a three-level check (`plain`
vs `-C` vs `-C -C -C`) returned an identical commit for every evidence row — so
this is a real null, not a knob that was too weak to matter. `--detect-moves`
stays opt-in and off by default; it costs ~8% runtime for no gain on this repo.

## Repo-shape caveat

35% of pairs are unanswerable because Kiro Crew is a de-Amazoned fork: its history
opens with large import commits and carries periodic bulk syncs, so blame on
older code lands on the porter. A repo with organic history should show a much
smaller `bulk_port` share. This is a property of the history, not of the engine —
but it does mean **`bulk_port` rate is the first thing to check when onboarding a
new repo**.

## Reproduce

```bash
python3 -m engine.cli batch --repo kirodotdev/KiroCrew \
  --repo-path /path/to/clone --limit 20 --out /tmp/att.jsonl
python3 -m tools.validate summary --jsonl /tmp/att.jsonl
python3 -m tools.validate table   --jsonl /tmp/att.jsonl
```

Runtime ~45s for 20 PRs (~2.2s each).
