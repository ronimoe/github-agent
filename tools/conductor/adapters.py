"""adapters — per-ecosystem pure-text VersionAdapters (P5, ADR-0005). Minimal anchored edits
(version line only; surrounding bytes unchanged), deterministic on re-run. No vendor tooling."""

from __future__ import annotations

import re


class VersionAdapter:
    name = ""

    def detect(self, filename: str) -> bool:
        raise NotImplementedError

    def read_version(self, content: str) -> str | None:
        raise NotImplementedError

    def write_version(self, content: str, new_version: str) -> str:
        raise NotImplementedError

    def tag_template(self, pkg: str, version: str) -> str:
        return f"{pkg}@{version}"


class NpmAdapter(VersionAdapter):
    name = "npm"

    def detect(self, fn):
        return fn.rsplit("/", 1)[-1] == "package.json"

    def read_version(self, content):
        m = re.search(r'"version"\s*:\s*"([^"]+)"', content)
        return m.group(1) if m else None

    def write_version(self, content, v):
        return re.sub(r'("version"\s*:\s*")[^"]+(")', lambda m: m.group(1) + v + m.group(2),
                      content, count=1)

    def tag_template(self, pkg, version):
        return f"v{version}"


class _TomlVersion(VersionAdapter):
    """First top-level `version = "x"` line (Cargo [package], PEP 621 [project])."""

    def read_version(self, content):
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', content)
        return m.group(1) if m else None

    def write_version(self, content, v):
        return re.sub(r'(?m)^(version\s*=\s*")[^"]+(")', lambda m: m.group(1) + v + m.group(2),
                      content, count=1)


class CargoAdapter(_TomlVersion):
    name = "cargo"

    def detect(self, fn):
        return fn.rsplit("/", 1)[-1] == "Cargo.toml"

    def tag_template(self, pkg, version):
        return f"v{version}"


class Pep621Adapter(_TomlVersion):
    name = "pep621"

    def detect(self, fn):
        return fn.rsplit("/", 1)[-1] == "pyproject.toml"


class GomodAdapter(VersionAdapter):
    name = "gomod"

    def detect(self, fn):
        return fn.rsplit("/", 1)[-1] == "go.mod"

    def read_version(self, content):
        return None                              # Go versions are git tags, not a manifest field

    def write_version(self, content, v):
        return content                            # nothing to write; the tag IS the version

    def tag_template(self, pkg, version):
        return f"v{version}"


class PlainAdapter(VersionAdapter):
    name = "plain"

    def detect(self, fn):
        return fn.rsplit("/", 1)[-1] == "VERSION"

    def read_version(self, content):
        return content.strip() or None

    def write_version(self, content, v):
        return v + "\n"

    def tag_template(self, pkg, version):
        return f"{pkg}-{version}"


ADAPTERS = [NpmAdapter(), CargoAdapter(), Pep621Adapter(), GomodAdapter(), PlainAdapter()]


def adapter_for(filename: str) -> VersionAdapter | None:
    return next((a for a in ADAPTERS if a.detect(filename)), None)
