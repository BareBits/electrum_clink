#!/usr/bin/env python3
"""Package the CLINK plugin as an Electrum external-plugin zip.

Electrum loads an external plugin by reading ``<dir>/manifest.json`` inside the
zip and importing it as ``electrum_external_plugins.<name>``; the package files
must sit under a top-level directory matching the plugin name. This script zips
the ``clink/`` package (with its ``manifest.json``) into ``dist/clink-<ver>.zip``
with exactly that layout.

NOTE: installing an external plugin also requires Electrum's in-app trust/
authorization step (it hashes the zip and records ``plugins.clink.authorized``).
For development the rig sidesteps all of this by symlinking the package in as a
normal internal plugin; this zip path is for eventual distribution and should be
verified against Electrum's external-plugin installer before release.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "clink"
DIST = ROOT / "dist"

EXCLUDE_SUFFIXES = (".pyc",)
EXCLUDE_DIRS = {"__pycache__"}


def build() -> Path:
    manifest = json.loads((PKG / "manifest.json").read_text())
    version = manifest.get("version", "0.0.0")
    name = manifest["name"]
    DIST.mkdir(exist_ok=True)
    out = DIST / f"{name}-{version}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(PKG.rglob("*")):
            if path.is_dir():
                if path.name in EXCLUDE_DIRS:
                    continue
                continue
            if any(part in EXCLUDE_DIRS for part in path.parts):
                continue
            if path.suffix in EXCLUDE_SUFFIXES:
                continue
            # arcname keeps the top-level "clink/" directory Electrum expects.
            arcname = path.relative_to(PKG.parent)
            zf.write(path, arcname)
    return out


if __name__ == "__main__":
    built = build()
    print(f"built {built} ({built.stat().st_size} bytes)")
