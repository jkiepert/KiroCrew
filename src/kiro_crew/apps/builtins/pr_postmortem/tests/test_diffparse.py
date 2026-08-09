"""Offline tests for pre-image extraction. No git, no network."""

from __future__ import annotations

import unittest

from kiro_crew.apps.builtins.pr_postmortem.engine.diffparse import (
    M_TEST,
    W_ANCHOR,
    W_MODIFIED,
    classify,
    parse_pre_image,
    total_signal,
)
from kiro_crew.apps.builtins.pr_postmortem.engine.vcs import pr_from_subject

MODIFY = """diff --git a/src/app/auth.py b/src/app/auth.py
index 1111111..2222222 100644
--- a/src/app/auth.py
+++ b/src/app/auth.py
@@ -42,3 +42,2 @@ def login(user):
-    bad_one
-    bad_two
-    bad_three
+    good_one
+    good_two
@@ -88 +87 @@ def logout(user):
-    stale
+    fresh
"""

PURE_INSERT = """diff --git a/src/app/guard.py b/src/app/guard.py
index 1111111..2222222 100644
--- a/src/app/guard.py
+++ b/src/app/guard.py
@@ -17,0 +18,2 @@ def handler(req):
+    if req is None:
+        return
"""

NEW_FILE = """diff --git a/test/test_guard.py b/test/test_guard.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/test/test_guard.py
@@ -0,0 +1,3 @@
+def test_guard():
+    assert True
"""

RENAME = """diff --git a/src/old_name.py b/src/new_name.py
similarity index 92%
rename from src/old_name.py
rename to src/new_name.py
index 1111111..2222222 100644
--- a/src/old_name.py
+++ b/src/new_name.py
@@ -5,2 +5,2 @@ import os
-    was_wrong
-    also_wrong
+    is_right
+    also_right
"""

LOCKFILE = """diff --git a/package-lock.json b/package-lock.json
index 1111111..2222222 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -900,4 +900,4 @@
-    "a": 1,
-    "b": 2,
-    "c": 3,
-    "d": 4,
+    "a": 9,
+    "b": 8,
+    "c": 7,
+    "d": 6,
"""

# A content line that itself starts with "---" must not be mistaken for a header.
TRICKY_CONTENT = """diff --git a/docs/architecture/design-notes/profiling.md b/docs/architecture/design-notes/profiling.md
index 1111111..2222222 100644
--- a/docs/architecture/design-notes/profiling.md
+++ b/docs/architecture/design-notes/profiling.md
@@ -3,2 +3,2 @@
----- old rule
-+++ old marker
+----- new rule
++++ new marker
"""


class TestParsePreImage(unittest.TestCase):
    def test_modified_ranges(self):
        files = parse_pre_image(MODIFY)
        self.assertEqual(len(files), 1)
        fc = files[0]
        self.assertEqual(fc.path, "src/app/auth.py")
        self.assertEqual(fc.kind, "source")
        # Second hunk uses the "@@ -88 +87 @@" single-line form (count omitted).
        self.assertEqual(fc.ranges, [(42, 3), (88, 1)])
        self.assertEqual(fc.anchors, [])
        self.assertEqual(fc.signal_weight(), 4 * W_MODIFIED)

    def test_pure_insertion_becomes_low_weight_anchor(self):
        fc = parse_pre_image(PURE_INSERT)[0]
        self.assertEqual(fc.ranges, [])
        self.assertEqual(fc.anchors, [17])
        self.assertEqual(fc.signal_weight(), W_ANCHOR)

    def test_new_file_is_excluded(self):
        fc = parse_pre_image(NEW_FILE)[0]
        self.assertTrue(fc.is_new_file)
        self.assertTrue(fc.excluded)
        self.assertEqual(total_signal([fc]), 0.0)

    def test_rename_blames_the_old_path(self):
        fc = parse_pre_image(RENAME)[0]
        self.assertEqual(fc.path, "src/old_name.py")
        self.assertEqual(fc.new_path, "src/new_name.py")
        self.assertEqual(fc.ranges, [(5, 2)])

    def test_lockfile_excluded(self):
        fc = parse_pre_image(LOCKFILE)[0]
        self.assertEqual(fc.kind, "generated")
        self.assertTrue(fc.excluded)
        self.assertEqual(total_signal([fc]), 0.0)

    def test_content_lines_are_not_parsed_as_headers(self):
        files = parse_pre_image(TRICKY_CONTENT)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].path, "docs/architecture/design-notes/profiling.md")
        self.assertEqual(files[0].ranges, [(3, 2)])

    def test_multi_file_diff(self):
        files = parse_pre_image(MODIFY + PURE_INSERT + NEW_FILE)
        self.assertEqual([f.path for f in files][:2], ["src/app/auth.py", "src/app/guard.py"])
        self.assertEqual(len(files), 3)
        self.assertAlmostEqual(total_signal(files), 4 * W_MODIFIED + W_ANCHOR)


class TestClassify(unittest.TestCase):
    def test_source(self):
        self.assertEqual(classify("src/kiro_crew/dashboard/state.py"), (1.0, "source"))

    def test_test_paths_downweighted(self):
        for path in (
            "test/test_dashboard.py",
            "tests/helpers/util.py",
            "website/src/x.test.tsx",
            "website/src/__tests__/y.ts",
            "pkg/thing_test.go",
        ):
            with self.subTest(path=path):
                self.assertEqual(classify(path), (M_TEST, "test"))

    def test_i18n_downweighted(self):
        self.assertEqual(classify("website/src/locales/ja.json")[1], "i18n")

    def test_generated_excluded(self):
        for path in ("package-lock.json", "website/dist/bundle.js", "a/b.min.js"):
            with self.subTest(path=path):
                self.assertEqual(classify(path)[0], 0.0)


class TestPrFromSubject(unittest.TestCase):
    def test_squash_subject(self):
        self.assertEqual(pr_from_subject("fix: guard voices (#1799)"), 1799)

    def test_trailing_whitespace_tolerated(self):
        self.assertEqual(pr_from_subject("fix: thing (#12)  \n"), 12)

    def test_issue_reference_mid_subject_is_not_a_pr(self):
        self.assertIsNone(pr_from_subject("fix: closes (#123) and more work"))

    def test_no_reference(self):
        self.assertIsNone(pr_from_subject("docs: add contributors"))


if __name__ == "__main__":
    unittest.main()
