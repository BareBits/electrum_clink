"""Shared pytest fixtures: load the JSON interop vectors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

VECTORS_DIR = Path(__file__).parent / "vectors"


@pytest.fixture(scope="session")
def noffer_vectors() -> List[Dict[str, Any]]:
    """Real noffer strings produced by @shocknet/clink-sdk."""
    with open(VECTORS_DIR / "noffer.vectors.json") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def nip44_vectors() -> Dict[str, Any]:
    """Official NIP-44 v2 test vectors (paulmillr/nip44)."""
    with open(VECTORS_DIR / "nip44.vectors.json") as f:
        return json.load(f)["v2"]
