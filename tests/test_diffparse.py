from triage.diffparse import parse_diff

DIFF = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,10 +1,13 @@
 import os
 
 def a():
-    return 1
+    return 2
 
 
 def b():
     pass
+
+
+def c():
+    return 3
"""


def test_splits_unrelated_edits_in_one_git_hunk():
    hunks = parse_diff(DIFF)
    spans = [(h.new_start, h.new_end) for h in hunks]
    assert len(hunks) == 2, spans
    assert hunks[0].added_lines == {4: "    return 2"}
    assert hunks[0].removed_lines == {4: "    return 1"}


def test_definition_boundary_splits_appended_block():
    diff = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,1 +1,7 @@
 x = 1
+
+def f():
+    return 1
+
+def g():
+    return 2
"""
    hunks = parse_diff(diff)
    assert [h.new_start for h in hunks] == [3, 6]


def test_new_and_deleted_files_are_flagged():
    diff = """diff --git a/n.py b/n.py
new file mode 100644
--- /dev/null
+++ b/n.py
@@ -0,0 +1,2 @@
+def f():
+    return 1
"""
    (hunk,) = parse_diff(diff)
    assert hunk.is_new_file and hunk.path == "n.py"


def test_pure_deletion_anchors_at_the_seam():
    diff = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,4 +1,2 @@
 a = 1
-b = 2
-c = 3
 d = 4
"""
    (hunk,) = parse_diff(diff)
    assert hunk.added_lines == {}
    assert sorted(hunk.removed_lines) == [2, 3]
    assert hunk.new_start >= 1
