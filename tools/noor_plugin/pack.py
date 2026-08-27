#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

from validate import validate

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".git", "dist", "cache", "tmp"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".sqlite", ".db"}
EXCLUDE_NAMES = {".DS_Store", "Thumbs.db"}


def should_include(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return False
    if path.name in EXCLUDE_NAMES:
        return False
    if path.suffix in EXCLUDE_SUFFIXES:
        return False
    return True


def read_manifest(plugin_dir: Path) -> dict:
    return json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))


def pack(plugin_dir: Path, output_dir: Path, force: bool, skip_validate: bool) -> Path:
    plugin_dir = plugin_dir.resolve()
    if not (plugin_dir / "plugin.json").exists():
        raise SystemExit(f"NOOR_PLUGIN_ERROR MANIFEST_MISSING {plugin_dir / 'plugin.json'} plugin.json 不存在")
    if not skip_validate:
        issues = validate(plugin_dir)
        errors = [i for i in issues if i.level == "ERROR"]
        if errors:
            for issue in issues:
                print(issue.line(), file=sys.stderr)
            raise SystemExit(1)
    manifest = read_manifest(plugin_dir)
    plugin_id = manifest.get("id") or plugin_dir.name
    version = manifest.get("version") or "0.0.0"
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"{plugin_id}-{version}.zip"
    if out.exists() and not force:
        raise SystemExit(f"NOOR_PLUGIN_ERROR PACK_EXISTS {out} already exists; use --force")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(plugin_dir.rglob("*")):
            if path.is_dir() or not should_include(path, plugin_dir):
                continue
            arcname = str(Path(plugin_id) / path.relative_to(plugin_dir))
            zf.write(path, arcname)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="noor-plugin pack")
    parser.add_argument("plugin_dir")
    parser.add_argument("--output-dir", "-o", default="dist/plugins")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-validate", action="store_true")
    args = parser.parse_args(argv)
    out = pack(Path(args.plugin_dir), Path(args.output_dir), args.force, args.skip_validate)
    print(f"NOOR_PLUGIN_PACKED {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
