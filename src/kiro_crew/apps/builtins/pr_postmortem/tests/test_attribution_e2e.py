"""End-to-end attribution against a real (synthetic) git repo.

Builds a tiny history where the culprit is known by construction, then asserts the
engine names it. gh is stubbed out, so the test is offline and hermetic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable

from kiro_crew.apps.builtins.pr_postmortem.engine import vcs
from kiro_crew.apps.builtins.pr_postmortem.engine.attribution import (
    STRONG_SHARE,
    WEAK_SHARE,
    attribute,
    compute_verdict,
)
from kiro_crew.apps.builtins.pr_postmortem.engine.discover import discover_fix_prs

BUGGY = """def handler(req):
    user = req.user
    token = user.token
    return validate(token)
"""

FIXED = """def handler(req):
    if req is None:
        return None
    user = req.user
    token = user.token if user else None
    return validate(token)
"""


def _git(args: list[str], cwd: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "author@example.invalid",
        },
    )


class TestAttributeEndToEnd(unittest.TestCase):
    # Assigned in setUpClass; declared so the type checker knows they exist.
    repo: str
    _real_gh: Callable[..., object]

    @classmethod
    def setUpClass(cls):
        cls.repo = tempfile.mkdtemp(prefix="prpm-e2e-")
        _git(["init", "-q", "-b", "main"], cls.repo)

        def write(rel: str, body: str) -> None:
            path = os.path.join(cls.repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)

        # (#100) introduces the buggy handler -- the culprit by construction.
        write("src/app.py", BUGGY)
        _git(["add", "-A"], cls.repo)
        _git(["commit", "-q", "-m", "feat: add request handler (#100)"], cls.repo)

        # (#101) unrelated churn elsewhere -- must NOT be blamed.
        write("src/other.py", "x = 1\ny = 2\n")
        _git(["add", "-A"], cls.repo)
        _git(["commit", "-q", "-m", "feat: unrelated helper (#101)"], cls.repo)

        # (#102) the fix: rewrites two of the buggy lines and inserts a guard.
        write("src/app.py", FIXED)
        _git(["add", "-A"], cls.repo)
        _git(["commit", "-q", "-m", "fix: guard missing request user (#102)"], cls.repo)

        cls._real_gh = vcs.gh_json
        vcs.gh_json = lambda *a, **k: None  # offline: no PR metadata lookups

    @classmethod
    def tearDownClass(cls):
        vcs.gh_json = cls._real_gh
        shutil.rmtree(cls.repo, ignore_errors=True)

    def setUp(self):
        vcs._PR_CACHE.clear()
        vcs._META_CACHE.clear()
        vcs._SIZE_CACHE.clear()

    def test_names_the_introducing_pr(self):
        att = attribute("owner/name", self.repo, fix_pr=102, branch="main")
        self.assertEqual(att.fix_pr, 102)
        self.assertTrue(att.fix_commit)
        self.assertTrue(att.candidates, msg=f"no candidates; notes={att.notes}")
        top = att.top
        assert top is not None  # candidates is non-empty, asserted above
        self.assertEqual(top.pr, 100)
        self.assertEqual(top.share, 1.0)
        self.assertNotIn("unmapped_commit", att.flags)
        self.assertNotIn(101, [c.pr for c in att.candidates])

    def test_evidence_is_reviewable(self):
        att = attribute("owner/name", self.repo, fix_pr=102, branch="main")
        self.assertTrue(att.evidence)
        row = att.evidence[0]
        self.assertEqual(row.file, "src/app.py")
        self.assertEqual(row.culprit_pr, 100)
        self.assertEqual(row.kind, "source")
        self.assertTrue(row.pre_image_lines)
        self.assertEqual(row.subject, "feat: add request handler (#100)")

    def test_fix_url_falls_back_without_gh(self):
        att = attribute("owner/name", self.repo, fix_pr=102, branch="main")
        self.assertEqual(att.fix_url, "https://github.com/owner/name/pull/102")

    def test_unknown_pr_reports_cleanly(self):
        att = attribute("owner/name", self.repo, fix_pr=9999, branch="main")
        self.assertEqual(att.verdict, "none")
        self.assertIn("fix_commit_not_found", att.flags)
        self.assertTrue(att.notes)

    def test_addition_only_fix_has_no_pre_image_signal(self):
        # A commit that only appends lines to a new region gives blame nothing to
        # chew on beyond the anchor -- assert we say so instead of inventing one.
        repo = tempfile.mkdtemp(prefix="prpm-add-")
        try:
            _git(["init", "-q", "-b", "main"], repo)
            with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as fh:
                fh.write("start\n")
            _git(["add", "-A"], repo)
            _git(["commit", "-q", "-m", "docs: seed (#1)"], repo)
            with open(os.path.join(repo, "NEW.md"), "w", encoding="utf-8") as fh:
                fh.write("brand new\n")
            _git(["add", "-A"], repo)
            _git(["commit", "-q", "-m", "fix: add missing doc (#2)"], repo)
            att = attribute("owner/name", repo, fix_pr=2, branch="main")
            self.assertEqual(att.verdict, "none")
            self.assertIn("no_pre_image_signal", att.flags)
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_discover_finds_only_fix_subjects(self):
        fixes = discover_fix_prs(self.repo, branch="main", limit=10)
        self.assertEqual([f.pr for f in fixes], [102])


class TestVerdictCalibration(unittest.TestCase):
    """Thresholds are calibrated against 20 hand-judged pairs on Kiro Crew.

    Each case below names the real fix PR it was derived from, so a future
    threshold change has to argue with measured data rather than intuition.
    """

    def test_clean_high_share_is_strong(self):
        # #1811, #2214, #2194, #2195: correct culprits, share >= 0.7
        self.assertEqual(compute_verdict(1.0, set()), "strong")
        self.assertEqual(compute_verdict(0.784, {"low_signal"}), "strong")

    def test_thin_evidence_does_not_block_strong(self):
        # #2194: one blamed line, single origin. Thin but right.
        self.assertEqual(compute_verdict(1.0, {"low_signal"}), "strong")

    def test_bulk_port_is_always_weak(self):
        # #1863/#1895/#1900/#2179: the fork-import commit MOVED the code here;
        # the real author is outside this repo at any share.
        self.assertEqual(compute_verdict(1.0, {"bulk_port"}), "weak")
        self.assertEqual(compute_verdict(0.972, {"bulk_port", "unmapped_commit"}), "weak")

    def test_diffuse_is_weak(self):
        # #1899 at 0.19: the fix spans code from several origins.
        self.assertEqual(compute_verdict(0.19, {"diffuse"}), "weak")

    def test_moderate_band_surfaces_real_hits(self):
        # #2187 (0.576), #2108 (0.62), #2223 (0.425), #2196 (0.446): all correct
        # culprits that must not be buried as weak.
        for share in (0.425, 0.446, 0.576, 0.62):
            with self.subTest(share=share):
                self.assertEqual(compute_verdict(share, set()), "moderate")

    def test_test_only_signal_is_weak(self):
        # #2184/#2206: only test files carried signal -- correct but unverifiable
        # as a product-code culprit.
        self.assertEqual(compute_verdict(1.0, {"no_source_signal"}), "weak")

    def test_large_commit_is_informational_not_blocking(self):
        # #2195 touched 76 files and was still the right culprit.
        self.assertEqual(compute_verdict(1.0, {"large_commit"}), "strong")

    def test_boundaries(self):
        self.assertEqual(compute_verdict(STRONG_SHARE, set()), "strong")
        self.assertEqual(compute_verdict(STRONG_SHARE - 0.001, set()), "moderate")
        self.assertEqual(compute_verdict(WEAK_SHARE, set()), "moderate")


if __name__ == "__main__":
    unittest.main()
