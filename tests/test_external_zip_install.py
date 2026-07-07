"""Regression test for installing the packaged zip via Electrum's real
external-plugin loader (Tools -> Plugins -> Add), rather than the dev rig.

The rig loads the plugin as an *internal* plugin (symlinked into
``electrum/plugins/``), so it never exercises the external-zip code path. That
path is where Electrum 4.7.x leaves the plugin package under the wrong module
identity, which used to break the first cross-module relative import with
``ModuleNotFoundError: No module named 'clink'``. ``clink/__init__.py`` repairs
that; this test guards the repair by driving the actual loader.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# The load must run in a *fresh* interpreter: Electrum registers the plugin's
# ConfigVars in a process-global registry, so importing the package a second
# time in a process that already imported ``clink`` (as the unit tests do) would
# trip a duplicate-key assertion unrelated to what we're testing.
_LOADER = r'''
import os, sys, shutil, tempfile
os.environ.pop("ELECTRUM_SKIP_PLUGIN_AUTH", None)   # a real GUI-style install

zip_path, gui = sys.argv[1], sys.argv[2]
from electrum.simple_config import SimpleConfig
from electrum import plugin as plugmod

# Build a Plugins instance by hand so we can point it at an *empty* internal
# plugins dir -- otherwise it would discover the dev rig's internal ``clink``
# symlink and never take the external-zip path we want to test.
config = SimpleConfig({"electrum_path": tempfile.mkdtemp()})
plugins = plugmod.Plugins.__new__(plugmod.Plugins)
plugmod.Logger.__init__(plugins)
plugins.config = config
plugins.cmd_only = False
plugins.gui_name = gui
plugins.plugins = {}
plugins.internal_plugin_metadata = {}
plugins.external_plugin_metadata = {}
plugins._hw_wallets = {}
plugins.pkgpath = tempfile.mkdtemp()                # empty internal plugins dir
plugmod.Plugins.add_jobs = lambda self, *a, **k: None
# The user performs the trust/authorize step via the GUI "Install" button; the
# signature check is orthogonal to module loading, so bypass just that here.
plugmod.Plugins.is_authorized = lambda self, name: True

pdir = plugins.get_external_plugin_dir()
shutil.copyfile(zip_path, os.path.join(pdir, os.path.basename(zip_path)))
plugins.find_zip_plugins(pkg_path=pdir, external=True)
assert plugins.is_external("clink"), "zip plugin was not discovered"
config.set_key("plugins.clink.enabled", True)

plugins.maybe_load_plugin_init_method("clink")
obj = plugins.load_plugin_by_name("clink")
assert obj is not None, "load_plugin_by_name returned None"

# Every submodule must resolve under the external namespace: this is what proves
# the cross-module relative imports (``from . import nip44, protocol`` etc.)
# resolved against the right package identity instead of a bare ``clink``.
cp = sys.modules["electrum_external_plugins.clink.clink_plugin"]
assert cp.__package__ == "electrum_external_plugins.clink", cp.__package__
print("LOADED_OK", type(obj).__module__)
'''


def _build_zip() -> Path:
    subprocess.run(
        [sys.executable, "scripts/build_zip.py"],
        cwd=REPO, check=True, capture_output=True, text=True,
    )
    zips = sorted((REPO / "dist").glob("clink-*.zip"))
    assert zips, "build_zip.py produced no zip"
    return zips[-1]


@pytest.mark.parametrize("gui", ["cmdline", "qt"])
def test_external_zip_loads_on_stock_electrum(gui: str) -> None:
    pytest.importorskip("electrum")
    if gui == "qt":
        pytest.importorskip("PyQt6")
    zip_path = _build_zip()
    proc = subprocess.run(
        [sys.executable, "-c", _LOADER, str(zip_path), gui],
        capture_output=True, text=True,
    )
    assert "LOADED_OK" in proc.stdout, (
        f"external zip failed to load for gui={gui!r}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
