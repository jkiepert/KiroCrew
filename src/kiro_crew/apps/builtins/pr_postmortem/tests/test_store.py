"""Tests for the report/decision store. Offline, isolated data dir."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from kiro_crew.apps.builtins.pr_postmortem.engine import store


def _rep(fix_pr: int, include_evidence: bool = True) -> dict:
    """Load a report and assert it exists, so tests can index it directly.

    ``load_report`` is legitimately Optional; asserting here keeps every call site
    honest instead of scattering ignores.
    """
    rep = store.load_report(fix_pr, include_evidence=include_evidence)
    assert rep is not None, f"expected a report for #{fix_pr}"
    return rep


ATTRIBUTION = {
    "fix_pr": 4242,
    "fix_title": "fix: guard the thing",
    "fix_url": "https://github.com/o/n/pull/4242",
    "fix_merged_at": "2026-08-01T00:00:00Z",
    "verdict": "strong",
    "confidence": 0.9,
    "flags": ["low_signal"],
    "signal_weight": 3.0,
    "candidates": [
        {
            "pr": 100,
            "subject": "feat: add the thing (#100)",
            "commits": ["abc123"],
            "largest_commit_files": 12,
        }
    ],
    "evidence": [{"file": "src/a.py", "pre_image_lines": "10", "culprit_pr": 100}],
    "notes": [],
}

ANALYSIS = {
    "fix_pr": 4242,
    "culprit_pr": 100,
    "culprit_link_verdict": "confirmed",
    "culprit_link_reason": "the culprit added the unguarded call",
    "root_cause_class": "error_handling_gap",
    "root_cause": "no guard around an optional dependency",
    "why_review_missed": "reviewers looked at the happy path",
    "why_tests_missed": "no test ran without the binary present",
    "proposals": [
        {
            "bucket": "gate",
            "title": "Add a CI job without the optional binary",
            "text": "Run the suite in an image lacking the binary.",
            "rationale": "would have failed on the unguarded call",
            "confidence": "high",
        },
        {
            "bucket": "test",
            "title": "Assert the absent-binary path returns 200",
            "text": "Patch shutil.which to None and assert the endpoint degrades.",
            "rationale": "locks in the guard",
            "confidence": "high",
        },
    ],
    "prompt_injection_observed": False,
    "notes": "",
}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prpm-store-")
        self._prev = os.environ.get("PRPM_DATA_DIR")
        os.environ["PRPM_DATA_DIR"] = self.tmp

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("PRPM_DATA_DIR", None)
        else:
            os.environ["PRPM_DATA_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, with_analysis: bool = True) -> None:
        store.save_attribution(ATTRIBUTION)
        if with_analysis:
            path = os.path.join(store.analysis_dir(), "analysis-4242.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(ANALYSIS, fh)


class TestDataDir(StoreTestCase):
    def test_override_wins(self):
        self.assertEqual(store.data_dir(), self.tmp)

    def test_kirocrew_home_used_when_no_override(self):
        # Capture and RESTORE the inherited value. Popping it unconditionally left
        # every later test resolving a different data home whenever the process had
        # one configured -- and this suite runs inside an agent session that does.
        prior_home = os.environ.get("KIROCREW_HOME")
        # Built from gettempdir(): the portability gate rejects absolute POSIX
        # path literals, and Windows has no such directory.
        fake_home = os.path.join(tempfile.gettempdir(), "prpm-fake-home")
        os.environ.pop("PRPM_DATA_DIR")
        os.environ["KIROCREW_HOME"] = fake_home
        try:
            self.assertEqual(
                store.data_dir(),
                os.path.join(fake_home, "workspace", "pr-postmortem"),
            )
        finally:
            if prior_home is None:
                os.environ.pop("KIROCREW_HOME", None)
            else:
                os.environ["KIROCREW_HOME"] = prior_home
            os.environ["PRPM_DATA_DIR"] = self.tmp


class TestReportMerge(StoreTestCase):
    def test_merge_attribution_and_analysis(self):
        self._seed()
        rep = _rep(4242)
        self.assertEqual(rep["culprit_pr"], 100)
        self.assertEqual(rep["verdict"], "strong")
        self.assertEqual(rep["root_cause_class"], "error_handling_gap")
        self.assertEqual(rep["link_verdict"], "confirmed")
        self.assertTrue(rep["analysis_present"])
        self.assertEqual(len(rep["proposals"]), 2)
        self.assertEqual(rep["proposals"][0]["id"], "4242:0")

    def test_report_without_analysis_is_still_readable(self):
        self._seed(with_analysis=False)
        rep = _rep(4242)
        self.assertFalse(rep["analysis_present"])
        self.assertEqual(rep["proposals"], [])
        self.assertIsNone(rep["root_cause_class"])

    def test_missing_report_is_none(self):
        self.assertIsNone(store.load_report(9999))

    def test_evidence_excluded_on_request(self):
        self._seed()
        self.assertIn("evidence", _rep(4242, include_evidence=True))
        self.assertNotIn("evidence", _rep(4242, include_evidence=False))

    def test_list_reports_counts_undecided(self):
        self._seed()
        rows = store.list_reports()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposals_total"], 2)
        self.assertEqual(rows[0]["proposals_undecided"], 2)
        self.assertEqual(rows[0]["proposal_buckets"], {"gate": 1, "test": 1})

    def test_list_reports_sorted_newest_first(self):
        self._seed()
        store.save_attribution({**ATTRIBUTION, "fix_pr": 9000})
        store.save_attribution({**ATTRIBUTION, "fix_pr": 1000})
        self.assertEqual([r["fix_pr"] for r in store.list_reports()], [9000, 4242, 1000])

    def test_non_numeric_files_ignored(self):
        self._seed()
        with open(os.path.join(store.reports_dir(), "notes.json"), "w") as fh:
            fh.write("{}")
        with open(os.path.join(store.reports_dir(), "baseline-x.jsonl"), "w") as fh:
            fh.write("{}\n")
        self.assertEqual([r["fix_pr"] for r in store.list_reports()], [4242])


class TestDecisions(StoreTestCase):
    def test_accept_persists_and_surfaces_on_the_report(self):
        self._seed()
        store.set_proposal_decision("4242:0", "accept", "will add the CI job")
        rep = _rep(4242)
        self.assertEqual(rep["proposals"][0]["decision"], "accept")
        self.assertEqual(rep["proposals"][0]["decision_note"], "will add the CI job")
        self.assertTrue(rep["proposals"][0]["decided_at"])
        self.assertIsNone(rep["proposals"][1]["decision"])
        self.assertEqual(store.list_reports()[0]["proposals_undecided"], 1)

    def test_rescan_must_not_destroy_a_human_decision(self):
        # The core durability claim: report files are rewritten by every scan, so
        # decisions live in their own file and must survive a full re-save.
        self._seed()
        store.set_proposal_decision("4242:1", "reject", "already covered")
        store.set_link_decision(4242, "not_a_culprit", "blame hit a mover")
        store.save_attribution({**ATTRIBUTION, "verdict": "moderate"})
        rep = _rep(4242)
        self.assertEqual(rep["verdict"], "moderate")
        self.assertEqual(rep["proposals"][1]["decision"], "reject")
        self.assertEqual(rep["human_link_decision"], "not_a_culprit")
        self.assertEqual(rep["human_link_note"], "blame hit a mover")

    def test_invalid_decision_rejected(self):
        with self.assertRaises(ValueError):
            store.set_proposal_decision("4242:0", "maybe")

    def test_malformed_proposal_id_rejected(self):
        for pid in ("4242", "4242:", ":0", "abc:0", "4242:x", "4242:0:1"):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                store.set_proposal_decision(pid, "accept")

    def test_invalid_link_decision_rejected(self):
        with self.assertRaises(ValueError):
            store.set_link_decision(4242, "confirmed_ish")

    def test_note_is_clipped(self):
        self._seed()
        saved = store.set_proposal_decision("4242:0", "defer", "x" * 5000)
        self.assertEqual(len(saved["note"]), 2000)

    def test_decision_overwrite_is_last_write_wins(self):
        self._seed()
        store.set_proposal_decision("4242:0", "accept")
        store.set_proposal_decision("4242:0", "reject", "changed my mind")
        rep = _rep(4242)
        self.assertEqual(rep["proposals"][0]["decision"], "reject")

    def test_writes_leave_no_temp_files_behind(self):
        self._seed()
        store.set_proposal_decision("4242:0", "accept")
        leftovers = [n for n in os.listdir(self.tmp) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TestProposalIds(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(store.parse_proposal_id(store.proposal_id(12, 3)), (12, 3))

    def test_bad_ids(self):
        for pid in ("", "12", "a:b", "12:", ":3"):
            with self.subTest(pid=pid):
                self.assertIsNone(store.parse_proposal_id(pid))


class TestImportJsonl(StoreTestCase):
    def test_import_skips_blank_and_malformed_lines(self):
        path = os.path.join(self.tmp, "batch.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(ATTRIBUTION) + "\n")
            fh.write("\n")
            fh.write("{not json}\n")
            fh.write(json.dumps({"no_fix_pr": 1}) + "\n")
            fh.write(json.dumps({**ATTRIBUTION, "fix_pr": 77}) + "\n")
        self.assertEqual(store.import_jsonl(path), 2)
        self.assertEqual([r["fix_pr"] for r in store.list_reports()], [4242, 77])


class TestState(StoreTestCase):
    def test_defaults_when_absent(self):
        st = store.load_state()
        self.assertEqual(st["repos"], [])
        self.assertIsNone(st["last_scan"])

    def test_touch_scan_records_summary(self):
        rec = store.touch_scan({"new_reports": 3})
        self.assertEqual(rec["new_reports"], 3)
        self.assertTrue(rec["at"])
        self.assertEqual(store.load_state()["last_scan"]["new_reports"], 3)


if __name__ == "__main__":
    unittest.main()
