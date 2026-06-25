"""apidiff — language-agnostic public-API surface differ for V1 semver inference (P4, ADR-0008).

A SYNTACTIC, per-language exported-symbol scanner. Inferred bump is a SOUND LOWER BOUND for
scanner-VISIBLE breaks only: removed/changed exported symbol => MAJOR, added => MINOR, else
PATCH/NONE. The verified fail-safes:

  * a changed file whose extension has NO registered scanner => UNCERTAIN (over-bump MAJOR + flag),
    NEVER NONE — a breaking change in an unsupported language must not infer no bump;
  * a file using re-export (`export ... from`, `pub use`), macros/codegen (`macro_rules!`,
    `__all__` manipulation, `export *`) that the scanner parses successfully => UNCERTAIN, so the
    lower bound is not silently defeated by an evasion;
  * a parse failure => UNCERTAIN.

UNCERTAIN does not change the chosen-bump monotonicity guarantee; it routes to over-bump + a
required human gate. It does NOT claim to catch behavioural breaks behind identical signatures —
that is the advisory reviewer's and the human gate's job.
"""

from __future__ import annotations

import ast
import re

NONE, PATCH, MINOR, MAJOR = 0, 1, 2, 3
LEVELS = {"none": NONE, "patch": PATCH, "minor": MINOR, "major": MAJOR}
NAMES = {v: k for k, v in LEVELS.items()}

SUPPORTED = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs"}
_SOURCE_EXT = SUPPORTED


def _py(src):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None, True
    syms, uncertain = {}, False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                syms[node.name] = tuple(a.arg for a in node.args.args)   # signature-sensitive
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                syms[node.name] = ()
        elif isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            uncertain = True
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            uncertain = True
    return syms, uncertain


def _regex(src, patterns, reexport_res):
    uncertain = any(r.search(src) for r in reexport_res)
    syms = {}
    for pat in patterns:
        for m in re.finditer(pat, src, re.M):
            syms[m.group(1)] = ()
    return syms, uncertain


_JS = ([r"^\s*export\s+(?:async\s+)?function\s+(\w+)",
        r"^\s*export\s+(?:const|let|var|class)\s+(\w+)",
        r"^\s*export\s+default\s+(?:function\s+)?(\w+)"],
       [re.compile(r"export\s+\*"), re.compile(r"export\s+\{[^}]*\}\s+from")])
_GO = ([r"^func\s+(?:\([^)]*\)\s+)?([A-Z]\w*)", r"^type\s+([A-Z]\w*)",
        r"^(?:var|const)\s+([A-Z]\w*)"], [])
_RS = ([r"^\s*pub\s+fn\s+(\w+)", r"^\s*pub\s+struct\s+(\w+)", r"^\s*pub\s+enum\s+(\w+)",
        r"^\s*pub\s+trait\s+(\w+)"],
       [re.compile(r"macro_rules!"), re.compile(r"^\s*pub\s+use\s", re.M), re.compile(r"include!")])


def _symbols(src, ext):
    if ext == ".py":
        return _py(src)
    if ext in (".js", ".ts", ".jsx", ".tsx"):
        return _regex(src, *_JS)
    if ext == ".go":
        return _regex(src, *_GO)
    if ext == ".rs":
        return _regex(src, *_RS)
    return None, True


def infer_file(old_src: str, new_src: str, ext: str) -> tuple[int, bool]:
    """(bump_level, uncertain) for one changed file."""
    if ext not in SUPPORTED:
        return MAJOR, True                          # unsupported language => fail closed (over-bump)
    old_syms, ou = _symbols(old_src, ext)
    new_syms, nu = _symbols(new_src, ext)
    if old_syms is None or new_syms is None:
        return MAJOR, True                          # parse failure => uncertain
    uncertain = ou or nu
    removed = set(old_syms) - set(new_syms)
    added = set(new_syms) - set(old_syms)
    changed = any(k in new_syms and old_syms[k] != new_syms[k] for k in set(old_syms) & set(new_syms))
    if removed or changed:
        return MAJOR, uncertain
    if added:
        return MINOR, uncertain
    return (PATCH if old_src != new_src else NONE), uncertain


def infer_bump(repo, base, merged_tree, changed) -> tuple[int, bool]:
    """Aggregate bump over a realized diff `changed` = [(status, path)]. Returns (level, uncertain)."""
    level, uncertain = NONE, False
    for status, path in changed:
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        if ext not in _SOURCE_EXT:
            continue                                # non-source change does not raise the API bump
        old_src = "" if status == "A" else (repo.read_blob(base, path) or "")
        new_src = "" if status == "D" else (repo.read_blob(merged_tree, path) or "")
        lv, unc = infer_file(old_src, new_src, ext)
        level = max(level, lv)
        uncertain = uncertain or unc
    return level, uncertain
