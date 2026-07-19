"""Load the repository-root Hermes/core package under its stable ``cassette`` name."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_core() -> ModuleType:
    existing = sys.modules.get("cassette")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "cassette",
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the Oh My Cassette core package")
    module = importlib.util.module_from_spec(spec)
    sys.modules["cassette"] = module
    spec.loader.exec_module(module)
    return module
