from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_cassette_package() -> None:
    if "cassette" in sys.modules:
        return
    # Keep web-demo configuration isolated from Hermes' ~/.hermes/.env fallback. Set this only when
    # the demo owns the process/package load; test suites may already have loaded the Hermes adapter.
    os.environ["CASSETTE_RUNTIME_ADAPTER"] = "web"
    spec = importlib.util.spec_from_file_location(
        "cassette",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cassette"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
