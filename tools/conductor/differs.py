"""differs — pluggable public-API differs + registry (issue #8). PURE: imports only ast/re (no
subprocess/socket — INV-8). External-toolchain differs live in shelldiff.py.

The default registry ships a deep stdlib Python AST differ (full signatures, defaults, return
annotations, exported class members) and the relocated syntactic fallbacks for js/ts/go/rust. The
soundness boundary is honest: the differ is a LOWER bound for *statically-visible* top-level surface;
re-exports / macros / codegen / dynamic exports force UNCERTAIN (→ over-bump MAJOR + human), never
NONE. Only a differ explicitly registered `authoritative=True` may tighten another's UNCERTAIN —
never inferred, so a weak scanner can never mask a real break.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from apidiff import NONE, PATCH, MINOR, MAJOR


@dataclass(frozen=True)
class ParamSig:
    name: str
    kind: str          # pos | pos_or_kw | var_pos | kw_only | var_kw
    has_default: bool
    default_repr: str | None
    annotation: str | None


@dataclass(frozen=True)
class SymbolSig:
    name: str
    kind: str          # function | class | var
    params: tuple = ()
    returns: str | None = None
    members: tuple = ()


@dataclass
class ApiDelta:
    level: int = NONE
    uncertain: bool = False
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


def _u(node) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node).replace(" ", "")
    except Exception:
        return "<unparseable>"


def _params(a: ast.arguments) -> tuple:
    out = []
    posonly, pos = list(a.posonlyargs), list(a.args)
    allpos = posonly + pos
    ndef = len(a.defaults)
    for i, arg in enumerate(allpos):
        has = i >= len(allpos) - ndef
        dval = a.defaults[i - (len(allpos) - ndef)] if has else None
        kind = "pos" if i < len(posonly) else "pos_or_kw"
        out.append(ParamSig(arg.arg, kind, has, _u(dval), _u(arg.annotation)))
    if a.vararg:
        out.append(ParamSig(a.vararg.arg, "var_pos", False, None, _u(a.vararg.annotation)))
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        out.append(ParamSig(arg.arg, "kw_only", d is not None, _u(d), _u(arg.annotation)))
    if a.kwarg:
        out.append(ParamSig(a.kwarg.arg, "var_kw", False, None, _u(a.kwarg.annotation)))
    return tuple(out)


def _exported(body) -> dict:
    syms = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                syms[node.name] = SymbolSig(node.name, "function", _params(node.args), _u(node.returns))
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                syms[node.name] = SymbolSig(node.name, "class", members=tuple(_class_members(node)))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not node.target.id.startswith("_"):
                syms[node.target.id] = SymbolSig(node.target.id, "var")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    syms[t.id] = SymbolSig(t.id, "var")
    return syms


def _class_members(cls: ast.ClassDef):
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            yield SymbolSig(node.name, "function", _params(node.args), _u(node.returns))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and not node.target.id.startswith("_"):
            yield SymbolSig(node.target.id, "var")


def _dynamic_markers(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            return True
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
            return True
        if isinstance(node, (ast.If, ast.Try, ast.While, ast.For)):
            for inner in ast.walk(node):
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                        and not inner.name.startswith("_"):
                    return True
    return False


def _member_level(old, new) -> int:
    od = {m.name: m for m in old}
    nd = {m.name: m for m in new}
    lvl = NONE
    if set(od) - set(nd):
        lvl = MAJOR
    for name in set(od) & set(nd):
        if od[name] != nd[name]:
            lvl = MAJOR
    if (set(nd) - set(od)) and lvl < MINOR:
        lvl = MINOR
    return lvl


def _compare(old_syms: dict, new_syms: dict) -> tuple[int, ApiDelta]:
    d = ApiDelta()
    removed = sorted(set(old_syms) - set(new_syms))
    added = sorted(set(new_syms) - set(old_syms))
    d.removed = removed
    d.added = added
    level = MAJOR if removed else NONE
    for name in sorted(set(old_syms) & set(new_syms)):
        o, n = old_syms[name], new_syms[name]
        if o.kind != n.kind:
            level = MAJOR
            d.changed.append(name)
        elif o.kind == "class":
            level = max(level, _member_level(o.members, n.members))
            if o.members != n.members:
                d.changed.append(name)
        elif o != n:
            level = MAJOR
            d.changed.append(name)
    if added and level < MINOR:
        level = MINOR
    d.level = level
    return level, d


class PyAstDiffer:
    language = "python"
    extensions = (".py",)

    def diff(self, old_src: str, new_src: str):
        try:
            old_tree, new_tree = ast.parse(old_src or ""), ast.parse(new_src or "")
        except SyntaxError:
            return MAJOR, True, ApiDelta(level=MAJOR, uncertain=True, reasons=["uncertain:parse-fail"])
        uncertain = _dynamic_markers(old_tree) or _dynamic_markers(new_tree)
        level, delta = _compare(_exported(old_tree.body), _exported(new_tree.body))
        if level == NONE and (old_src or "") != (new_src or ""):
            level = PATCH                                  # impl-only change to exported surface
        delta.level = level
        delta.uncertain = uncertain
        if uncertain:
            delta.reasons.append("uncertain:dynamic-export")
        return level, uncertain, delta


class SyntacticDiffer:
    """Relocated regex scanners (js/ts/go/rust) — a terminal FALLBACK. UNTRUSTWORTHY for codegen:
    out-of-file codegen leaves no in-file marker, so its NONE/MINOR cannot be trusted; biased to
    UNCERTAIN on any re-export/macro marker. Never marketed as catching evasions."""

    def __init__(self, language, extensions, patterns, reexport_res):
        self.language = language
        self.extensions = tuple(extensions)
        self._pats = [re.compile(p, re.M) for p in patterns]
        self._reexport = [re.compile(r) for r in reexport_res]

    def _symbols(self, src):
        out = set()
        for rx in self._pats:
            out.update(m.group(1) for m in rx.finditer(src))
        return out

    def diff(self, old_src, new_src):
        uncertain = any(r.search(old_src or "") or r.search(new_src or "") for r in self._reexport)
        old, new = self._symbols(old_src or ""), self._symbols(new_src or "")
        removed, added = old - new, new - old
        level = MAJOR if removed else (MINOR if added else (PATCH if (old_src or "") != (new_src or "") else NONE))
        d = ApiDelta(level=level, uncertain=uncertain, removed=sorted(removed), added=sorted(added))
        if uncertain:
            d.reasons.append("uncertain:dynamic-export")
        return level, uncertain, d


def _js():
    return SyntacticDiffer("javascript", (".js", ".ts", ".jsx", ".tsx"),
                           [r"^\s*export\s+(?:async\s+)?function\s+(\w+)",
                            r"^\s*export\s+(?:const|let|var|class)\s+(\w+)",
                            r"^\s*export\s+default\s+(?:function\s+)?(\w+)"],
                           [r"export\s+\*", r"export\s+\{[^}]*\}\s+from"])


def _go():
    return SyntacticDiffer("go", (".go",),
                           [r"^func\s+(?:\([^)]*\)\s+)?([A-Z]\w*)", r"^type\s+([A-Z]\w*)",
                            r"^(?:var|const)\s+([A-Z]\w*)"], [])


def _rs():
    return SyntacticDiffer("rust", (".rs",),
                           [r"^\s*pub\s+fn\s+(\w+)", r"^\s*pub\s+struct\s+(\w+)",
                            r"^\s*pub\s+enum\s+(\w+)", r"^\s*pub\s+trait\s+(\w+)"],
                           [r"macro_rules!", r"^\s*pub\s+use\s", r"include!"])


class DifferRegistry:
    def __init__(self):
        self._differs = []          # list[(differ, authoritative)]

    def register(self, differ, *, authoritative: bool = False):
        self._differs.append((differ, authoritative))
        return self

    def _safe(self, differ, old, new):
        try:
            return differ.diff(old, new)
        except Exception:
            return MAJOR, True, ApiDelta(level=MAJOR, uncertain=True, reasons=["uncertain:differ-crash"])

    def diff_file(self, old_src, new_src, ext) -> tuple[int, bool, ApiDelta]:
        chain = [(d, a) for d, a in self._differs if ext in d.extensions]
        if not chain:
            return MAJOR, True, ApiDelta(level=MAJOR, uncertain=True, reasons=["uncertain:unsupported-ext"])
        level, uncertain, delta = NONE, False, None
        auth_verdict = None
        for differ, authoritative in chain:
            lv, unc, dl = self._safe(differ, old_src, new_src)
            delta = dl
            if authoritative and not unc:
                auth_verdict = (lv, False)          # only an authoritative+certain differ tightens
            else:
                level = max(level, lv)
                uncertain = uncertain or unc
        if auth_verdict is not None:
            return auth_verdict[0], auth_verdict[1], delta
        return level, uncertain, delta


def default_registry() -> DifferRegistry:
    r = DifferRegistry()
    r.register(PyAstDiffer())                       # none authoritative by default (no compiler backing)
    r.register(_js()); r.register(_go()); r.register(_rs())
    return r
