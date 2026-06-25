"""apidiff — public seam for V1 semver inference (P4, issue #8).

The bump level enum and the public functions `infer_file` / `infer_bump` are UNCHANGED (verifier.py,
changeset.py, semver.py, test_p4 depend on them). The per-language analysis moved to `differs.py`
(pure) + `shelldiff.py` (external toolchains); this module is the registry-backed FOLD.

The blocking fix (#8): the old fold SKIPPED any unsupported extension to NONE — a breaking change in
an unscanned language landed as no-bump (fail OPEN). Now only an explicit NON-API allowlist is
skipped; every other (plausibly-code) file with no claiming differ folds to MAJOR + UNCERTAIN
(over-bump + human), never NONE.
"""

from __future__ import annotations

NONE, PATCH, MINOR, MAJOR = 0, 1, 2, 3
LEVELS = {"none": NONE, "patch": PATCH, "minor": MINOR, "major": MAJOR}
NAMES = {v: k for k, v in LEVELS.items()}

# Files that are definitively NOT public API — the ONLY things skipped. Everything else routes to a
# differ (or fails closed to UNCERTAIN-MAJOR).
NON_API_EXT = {".md", ".markdown", ".rst", ".txt", ".lock", ".png", ".jpg", ".jpeg", ".gif",
               ".svg", ".ico", ".pdf", ".csv", ".json", ".yaml", ".yml", ".toml", ".cfg",
               ".ini", ".html", ".css", ".sh"}
NON_API_BASENAMES = {"LICENSE", "NOTICE", "CHANGELOG", ".gitignore", ".gitattributes",
                     "Makefile", "Dockerfile", "Procfile"}

_REGISTRY = None


def _registry():
    global _REGISTRY
    if _REGISTRY is None:
        from differs import default_registry       # lazy to avoid an import cycle
        _REGISTRY = default_registry()
    return _REGISTRY


def _ext(path: str) -> tuple[str, str]:
    base = path.rsplit("/", 1)[-1]
    return base, ("." + base.rsplit(".", 1)[-1] if "." in base else "")


def infer_file(old_src: str, new_src: str, ext: str) -> tuple[int, bool]:
    lv, unc, _ = _registry().diff_file(old_src, new_src, ext)
    return lv, unc


def infer_bump_ex(repo, base, merged_tree, changed) -> tuple[int, bool, list]:
    level, uncertain, deltas = NONE, False, []
    reg = _registry()
    for status, path in changed:
        base_name, ext = _ext(path)
        if ext in NON_API_EXT or base_name in NON_API_BASENAMES:
            continue                                # the ONLY skip — explicit non-API allowlist
        old_src = "" if status == "A" else (repo.read_blob(base, path) or "")
        new_src = "" if status == "D" else (repo.read_blob(merged_tree, path) or "")
        lv, unc, delta = reg.diff_file(old_src, new_src, ext)
        level = max(level, lv)
        uncertain = uncertain or unc
        deltas.append(delta)
    return level, uncertain, deltas


def infer_bump(repo, base, merged_tree, changed) -> tuple[int, bool]:
    level, uncertain, _ = infer_bump_ex(repo, base, merged_tree, changed)
    return level, uncertain
