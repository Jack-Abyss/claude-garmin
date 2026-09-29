"""
Consistency check for the Claude Desktop extension (.mcpb) packaging.

Fails if manifest.json drifts from the code it ships:
  - every @mcp.tool() in garmin_mcp.py is listed in manifest.json and vice versa
  - manifest.json and pyproject.toml carry the same version
  - requirements.txt and pyproject.toml declare the same dependencies

Stdlib only (Python 3.11+ for tomllib), so CI can run it without installing
anything:

    python scripts/check_manifest.py
"""
from __future__ import annotations

import ast
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _server_tools() -> set[str]:
    tree = ast.parse((ROOT / "garmin_mcp.py").read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            func = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(func, ast.Attribute) and func.attr == "tool":
                names.add(node.name)
    return names


def main() -> int:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = {
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    errors = []

    listed = {t["name"] for t in manifest.get("tools", [])}
    actual = _server_tools()
    if missing := actual - listed:
        errors.append(f"tools missing from manifest.json: {sorted(missing)}")
    if stale := listed - actual:
        errors.append(f"manifest.json lists tools the server lacks: {sorted(stale)}")

    if manifest["version"] != pyproject["project"]["version"]:
        errors.append(
            f"version mismatch: manifest.json {manifest['version']} vs "
            f"pyproject.toml {pyproject['project']['version']}"
        )

    deps = set(pyproject["project"]["dependencies"])
    if deps != requirements:
        errors.append(
            f"dependency mismatch: pyproject.toml {sorted(deps)} vs "
            f"requirements.txt {sorted(requirements)}"
        )

    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    if not errors:
        print(f"OK: {len(actual)} tools, version {manifest['version']}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
