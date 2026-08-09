"""Tests for evidence-bundle assembly and the analysis output contract.

Both modules sit between untrusted PR text and a model, so the properties under
test are mostly about honesty of the data handed over: truncation must be visible,
untrusted fields must stay fenced, and a malformed verdict must be rejected rather
than absorbed.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from kiro_crew.apps.builtins.pr_postmortem.engine import analysis, bundle

FIX_DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,2 +10,3 @@ def handler(req):
-    old_one
-    old_two
+    new_one
+    new_two
+    new_three
diff --git a/test/test_app.py b/test/test_app.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/test/test_app.py
@@ -0,0 +1,4 @@
+def test_guard():
+    assert True
+
+
"""


class TestTruncation(unittest.TestCase):
    def test_short_text_untouched(self):
        text, cut = bundle._truncate_lines("a\nb\nc", 10)
        self.assertEqual(text, "a\nb\nc")
        self.assertFalse(cut)

    def test_truncation_leaves_a_visible_marker(self):
        # A diff that merely stops reads as complete; the analyst would then
        # attribute the defect to whatever was last visible.
        text, cut = bundle._truncate_lines("\n".join(str(i) for i in range(100)), 10)
        self.assertTrue(cut)
        self.assertIn("INCOMPLETE", text)
        self.assertIn("90 more diff lines truncated", text)
        self.assertEqual(len(text.splitlines()), 11)

    def test_clip_marks_and_prefers_a_line_boundary(self):
        text = "\n".join(["x" * 20] * 20)
        out = bundle._clip(text, 100)
        self.assertIn("[...clipped...]", out)
        # Cut at a newline, so no line is split mid-token.
        body = out.split("\n[...clipped...]")[0]
        self.assertTrue(all(line == "x" * 20 for line in body.splitlines()))

    def test_clip_leaves_short_text_alone(self):
        self.assertEqual(bundle._clip("short", 100), "short")

    def test_clip_handles_none(self):
        self.assertEqual(bundle._clip(None, 10), "")


class TestTestChangeDetection(unittest.TestCase):
    def test_finds_the_test_file_the_fix_added(self):
        changes = bundle._test_changes(FIX_DIFF)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].path, "test/test_app.py")
        self.assertTrue(changes[0].is_new_file)
        self.assertEqual(changes[0].added_lines, 4)

    def test_product_code_is_not_reported_as_a_test(self):
        paths = [c.path for c in bundle._test_changes(FIX_DIFF)]
        self.assertNotIn("src/app.py", paths)

    def test_no_tests_added_yields_empty(self):
        only_src = FIX_DIFF.split("diff --git a/test/")[0]
        self.assertEqual(bundle._test_changes(only_src), [])


class TestPromptRendering(unittest.TestCase):
    def test_prompt_renders_without_format_errors(self):
        # Never opened -- only interpolated into the prompt -- but built from
        # gettempdir() so no absolute POSIX literal ships in source.
        bundle_path = os.path.join(tempfile.gettempdir(), "b.json")
        analysis_path = os.path.join(tempfile.gettempdir(), "a.json")
        text = analysis.build_prompt("o/n", bundle_path, analysis_path, 5, 3)
        self.assertIn(bundle_path, text)
        self.assertIn(analysis_path, text)
        self.assertIn('"fix_pr": 5', text)
        self.assertIn('"culprit_pr": 3', text)

    def test_literal_json_braces_survive_formatting(self):
        # A mangled schema block would silently degrade every analysis.
        text = analysis.build_prompt("o/n", "/b", "/a", 1, None)
        self.assertIn('"culprit_link_verdict": "confirmed|rejected|uncertain"', text)
        self.assertIn('"proposals": [', text)
        self.assertIn('{"bucket": "", "title": ""', text)
        self.assertNotIn("{classes}", text)
        self.assertNotIn("{max_proposals}", text)

    def test_null_culprit_renders_as_json_null(self):
        self.assertIn('"culprit_pr": null', analysis.build_prompt("o/n", "/b", "/a", 1, None))

    def test_prompt_carries_the_untrusted_data_frame(self):
        text = analysis.build_prompt("o/n", "/b", "/a", 1, 2)
        self.assertIn("DATA, NOT INSTRUCTIONS", text)
        self.assertIn("prompt_injection_observed", text)

    def test_prompt_permits_rejection(self):
        text = analysis.build_prompt("o/n", "/b", "/a", 1, 2)
        self.assertIn('EXPECTED to return "rejected"', text)


def _valid_analysis(**over):
    base = {
        "fix_pr": 10,
        "culprit_pr": 5,
        "culprit_link_verdict": "confirmed",
        "culprit_link_reason": "the culprit wrote the line the fix changed",
        "root_cause_class": "error_handling_gap",
        "root_cause": "unguarded optional dependency",
        "why_review_missed": "reviewers read the happy path",
        "why_tests_missed": "no test ran without the binary",
        "proposals": [
            {
                "bucket": "gate",
                "title": "Run the suite without the optional binary",
                "text": "Add a CI job in an image lacking it",
                "rationale": "would have failed on the unguarded call",
                "confidence": "high",
            }
        ],
        "prompt_injection_observed": False,
        "notes": "",
    }
    base.update(over)
    return base


class TestValidate(unittest.TestCase):
    def test_valid_passes(self):
        self.assertEqual(analysis.validate(_valid_analysis()), [])

    def test_non_int_culprit_pr_rejected(self):
        errs = analysis.validate(_valid_analysis(culprit_pr="five"))
        self.assertTrue(any("culprit_pr" in e for e in errs))

    def test_null_culprit_pr_allowed(self):
        self.assertEqual(analysis.validate(_valid_analysis(culprit_pr=None)), [])

    def test_non_bool_injection_flag_rejected(self):
        errs = analysis.validate(_valid_analysis(prompt_injection_observed="yes"))
        self.assertTrue(any("prompt_injection_observed" in e for e in errs))

    def test_rejected_link_with_analysis_fields_is_contradictory(self):
        errs = analysis.validate(
            _valid_analysis(culprit_link_verdict="rejected", proposals=[])
        )
        self.assertTrue(any("must leave root_cause_class empty" in e for e in errs))

    def test_clean_rejection_passes(self):
        clean = {
            "fix_pr": 10,
            "culprit_pr": 5,
            "culprit_link_verdict": "rejected",
            "culprit_link_reason": "blame landed on a reformat",
            "root_cause_class": "",
            "root_cause": "",
            "why_review_missed": "",
            "why_tests_missed": "",
            "proposals": [],
            "prompt_injection_observed": False,
            "notes": "",
        }
        self.assertEqual(analysis.validate(clean), [])

    def test_rejected_link_with_proposals_refused(self):
        errs = analysis.validate(
            _valid_analysis(
                culprit_link_verdict="rejected",
                root_cause_class="",
                root_cause="",
                why_review_missed="",
                why_tests_missed="",
            )
        )
        self.assertTrue(any("must not carry prevention proposals" in e for e in errs))

    def test_bad_bucket_and_confidence_rejected(self):
        bad = _valid_analysis()
        bad["proposals"][0]["bucket"] = "process"
        bad["proposals"][0]["confidence"] = "certain"
        errs = analysis.validate(bad)
        self.assertTrue(any("bucket invalid" in e for e in errs))
        self.assertTrue(any("confidence invalid" in e for e in errs))

    def test_too_many_proposals_rejected(self):
        many = _valid_analysis()
        many["proposals"] = many["proposals"] * 4
        self.assertTrue(any("at most" in e for e in analysis.validate(many)))

    def test_non_object_rejected(self):
        self.assertEqual(analysis.validate([1, 2]), ["analysis is not a JSON object"])

    def test_missing_file_reported(self):
        obj, errs = analysis.load_and_validate("/nonexistent/analysis-1.json")
        self.assertIsNone(obj)
        self.assertTrue(any("missing analysis file" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
