#!/usr/bin/env python3
"""#8 tests: the pluggable API-differ registry. The fold no longer fails OPEN on unsupported code
exts; the Python AST differ catches signature/default/return/member changes and forces UNCERTAIN on
dynamic exports; the authority asymmetry is un-bypassable; the ShellDiffer is fail-safe."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitutil  # noqa: E402
from gitutil import WorkRepo  # noqa: E402
import apidiff  # noqa: E402
from apidiff import NONE, PATCH, MINOR, MAJOR, infer_file, infer_bump  # noqa: E402
import differs  # noqa: E402
from differs import PyAstDiffer, DifferRegistry, ApiDelta  # noqa: E402
from shelldiff import ShellDiffer, ShellDifferSpec  # noqa: E402


def _bare():
    d = tempfile.mkdtemp(prefix="conductor-bare-")
    subprocess.run(["git", "init", "--bare", "-q", d], check=True)
    return d


def _f(old, new):
    return infer_file(old, new, ".py")


class PyAstTests(unittest.TestCase):
    def test_removed_added_signature(self):
        self.assertEqual(_f("def foo():\n pass\ndef bar():\n pass\n", "def foo():\n pass\n")[0], MAJOR)
        self.assertEqual(_f("def foo():\n pass\n", "def foo():\n pass\ndef baz():\n pass\n")[0], MINOR)
        self.assertEqual(_f("def foo(a):\n pass\n", "def foo(a, b):\n pass\n")[0], MAJOR)

    def test_default_return_annotation_changes(self):
        self.assertEqual(_f("def f(a, b=1):\n pass\n", "def f(a, b=2):\n pass\n")[0], MAJOR)   # default value
        self.assertEqual(_f("def f(a, b=1):\n pass\n", "def f(a, b):\n pass\n")[0], MAJOR)      # default removed
        self.assertEqual(_f("def f()->int:\n pass\n", "def f()->str:\n pass\n")[0], MAJOR)      # return
        self.assertEqual(_f("def f()->int:\n pass\n", "def f():\n pass\n")[0], MAJOR)           # return removed
        self.assertEqual(_f("def f(a:int):\n pass\n", "def f(a:str):\n pass\n")[0], MAJOR)       # annotation

    def test_kwonly_varargs_posonly(self):
        self.assertEqual(_f("def f(a):\n pass\n", "def f(a, *, k):\n pass\n")[0], MAJOR)         # kw-only added
        self.assertEqual(_f("def f(a, b):\n pass\n", "def f(b, a):\n pass\n")[0], MAJOR)         # reorder/rename
        self.assertEqual(_f("def f(*args):\n pass\n", "def f():\n pass\n")[0], MAJOR)            # *args removed
        self.assertEqual(_f("def f(a, b):\n pass\n", "def f(a, /, b):\n pass\n")[0], MAJOR)       # posonly separator

    def test_class_members(self):
        self.assertEqual(_f("class C:\n def m(self):\n  pass\n", "class C:\n pass\n")[0], MAJOR)   # removed method
        self.assertEqual(_f("class C:\n def m(self):\n  pass\n",
                            "class C:\n def m(self):\n  pass\n def n(self):\n  pass\n")[0], MINOR)  # added method
        self.assertEqual(_f("class C:\n def _p(self):\n  return 1\n",
                            "class C:\n def _p(self):\n  return 2\n")[0], PATCH)                    # private churn
        self.assertEqual(_f("class C:\n def m(self):\n  pass\n", "class C:\n def m(self):\n  pass\n")[0], NONE)

    def test_dynamic_export_forces_uncertain(self):
        for new in ("def f():\n pass\nfrom x import *\n",
                    "def f():\n pass\n__all__ = ['f']\n",
                    "def f():\n pass\ndef __getattr__(n):\n return 1\n",
                    "if True:\n def g():\n  pass\n"):
            self.assertTrue(_f("def f():\n pass\n", new)[1], new)   # uncertain

    def test_parse_fail_uncertain_not_crash(self):
        lv, unc = _f("def f(:\n", "def f():\n pass\n")
        self.assertEqual((lv, unc), (MAJOR, True))

    def test_differ_crash_caught_at_registry(self):
        class Boom:
            language = "x"; extensions = (".x",)
            def diff(self, o, n):
                raise RecursionError("boom")
        reg = DifferRegistry().register(Boom())
        self.assertEqual(reg.diff_file("a", "b", ".x")[:2], (MAJOR, True))


class SyntacticTests(unittest.TestCase):
    def test_reexport_uncertain(self):
        self.assertTrue(infer_file("export const a = 1\n", "export const a = 1\nexport * from './o'\n", ".ts")[1])
        self.assertTrue(infer_file("pub fn a(){}\n", "pub fn a(){}\nmacro_rules! m {}\n", ".rs")[1])


class FoldTests(unittest.TestCase):
    @unittest.skipUnless(gitutil.git_available() and gitutil.has_write_tree_merge(), "git >= 2.38")
    def test_unsupported_code_ext_is_major_not_skipped(self):
        w = WorkRepo(_bare())
        base = w.write_commit({"a.rb": "def foo; end\n"}, parent=None)
        head = w.write_commit({"a.rb": "def bar; end\n"}, parent=base)
        _, tree = w.merge_tree(base, head)
        changed = w.diff_name_status(base, tree)
        self.assertEqual(infer_bump(w, base, tree, changed), (MAJOR, True))   # FIX-1: NOT skipped to NONE

    @unittest.skipUnless(gitutil.git_available() and gitutil.has_write_tree_merge(), "git >= 2.38")
    def test_non_api_file_skipped(self):
        w = WorkRepo(_bare())
        base = w.write_commit({"README.md": "# x\n"}, parent=None)
        head = w.write_commit({"README.md": "# y\n"}, parent=base)
        _, tree = w.merge_tree(base, head)
        self.assertEqual(infer_bump(w, base, tree, w.diff_name_status(base, tree)), (NONE, False))

    @unittest.skipUnless(gitutil.git_available() and gitutil.has_write_tree_merge(), "git >= 2.38")
    def test_fold_monotone_mixed(self):
        w = WorkRepo(_bare())
        base = w.write_commit({"x.py": "def foo():\n pass\n", "k.kt": "fun a(){}\n"}, parent=None)
        head = w.write_commit({"x.py": "def foo():\n pass\ndef bar():\n pass\n", "k.kt": "fun b(){}\n"}, parent=base)
        _, tree = w.merge_tree(base, head)
        self.assertEqual(infer_bump(w, base, tree, w.diff_name_status(base, tree)), (MAJOR, True))  # .kt -> uncertain


class AuthorityTests(unittest.TestCase):
    def test_non_authoritative_cannot_clear_uncertain(self):
        class Fallback:
            language = "f"; extensions = (".z",)
            def diff(self, o, n):
                return MAJOR, True, ApiDelta(level=MAJOR, uncertain=True)

        class Weak:
            language = "w"; extensions = (".z",)
            def diff(self, o, n):
                return NONE, False, ApiDelta(level=NONE)
        reg = DifferRegistry().register(Fallback()).register(Weak())     # weak is NOT authoritative
        self.assertEqual(reg.diff_file("a", "b", ".z")[:2], (MAJOR, True))

    def test_authoritative_tightens(self):
        class Fallback:
            language = "f"; extensions = (".z",)
            def diff(self, o, n):
                return MAJOR, True, ApiDelta(level=MAJOR, uncertain=True)

        class Auth:
            language = "a"; extensions = (".z",)
            def diff(self, o, n):
                return MINOR, False, ApiDelta(level=MINOR)
        reg = DifferRegistry().register(Fallback()).register(Auth(), authoritative=True)
        self.assertEqual(reg.diff_file("a", "b", ".z")[:2], (MINOR, False))   # authoritative resolves


class PurityTests(unittest.TestCase):
    def test_differs_has_no_subprocess(self):
        self.assertFalse(hasattr(differs, "subprocess"))
        self.assertFalse(hasattr(differs, "socket"))


class ShellDifferTests(unittest.TestCase):
    def _spec(self, parse):
        return ShellDifferSpec("rust", (".rs",), ("cargo", "--version"), ("cargo", "diff"), parse)

    def test_missing_toolchain_uncertain(self):
        sd = ShellDiffer(self._spec(lambda rc, o, e: ApiDelta(level=NONE)),
                         run=lambda *a, **k: (0, "", ""), which=lambda b: False)
        self.assertEqual(sd.diff_tree(None, None, None, [])[:2], (MAJOR, True))

    def test_break_reported_with_certainty(self):
        def run(argv, *a, **k):
            return (0, "", "") if "--version" in argv else (1, "removed foo", "")  # probe ok, build=break
        sd = ShellDiffer(self._spec(lambda rc, o, e: ApiDelta(level=MAJOR, removed=["foo"])),
                         run=run, which=lambda b: True, hermetic=True)
        self.assertEqual(sd.diff_tree(None, None, None, [])[:2], (MAJOR, False))

    def test_non_hermetic_none_capped_to_uncertain(self):
        sd = ShellDiffer(self._spec(lambda rc, o, e: ApiDelta(level=NONE)),
                         run=lambda *a, **k: (0, "no change", ""), which=lambda b: True, hermetic=False)
        self.assertEqual(sd.diff_tree(None, None, None, [])[:2], (NONE, True))    # capped

    def test_timeout_and_unparseable_uncertain(self):
        def boom(*a, **k):
            if "--version" in a[0]:
                return (0, "", "")
            raise subprocess.TimeoutExpired("cargo", 600)
        self.assertEqual(ShellDiffer(self._spec(lambda *a: None), run=boom, which=lambda b: True)
                         .diff_tree(None, None, None, [])[:2], (MAJOR, True))
        unp = ShellDiffer(self._spec(lambda rc, o, e: None),
                          run=lambda *a, **k: (0, "garbage", ""), which=lambda b: True)
        self.assertEqual(unp.diff_tree(None, None, None, [])[:2], (MAJOR, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
