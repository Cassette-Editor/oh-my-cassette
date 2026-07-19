from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_cassette_package() -> None:
    if "cassette" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "cassette", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cassette"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)


_load_cassette_package()


def pytest_collection_modifyitems(config, items):
    if os.getenv("RUN_CASSETTE_E2E") == "1":
        return
    skip_e2e = pytest.mark.skip(reason="set RUN_CASSETTE_E2E=1 to run real gateway/Cassette E2E tests")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)


@pytest.fixture(autouse=True)
def _default_browser_transport(monkeypatch):
    # The plugin ships with CASSETTE_TRANSPORT=api as the default. Pin the suite to the browser path
    # (what most tests were written against) so they exercise the Playwright entrypoints; tests that
    # target the API transport override this by setting CASSETTE_TRANSPORT=api (or delete it to assert
    # the shipped default).
    monkeypatch.setenv("CASSETTE_TRANSPORT", "browser")


@pytest.fixture
def cassette_env(tmp_path, monkeypatch):
    asset_root = tmp_path / "asset-root"
    source_root = tmp_path / "source-root"
    source_root.mkdir()
    monkeypatch.setenv("CASSETTE_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv("CASSETTE_ALLOWED_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("CASSETTE_ALLOWED_EXTENSIONS", ".mp4,.jpg,.png,.mp3,.txt")
    monkeypatch.setenv("CASSETTE_MAX_BYTES", "1024")
    monkeypatch.setenv("CASSETTE_MIN_BROWSER_TIMEOUT_SEC", "0")
    monkeypatch.setenv("CASSETTE_WEIXIN_FORCE_H264", "0")
    monkeypatch.setenv("CASSETTE_PING_ON_GATEWAY_INSTRUCTION", "0")
    monkeypatch.setenv("CASSETTE_GATEWAY_MODEL_CHOICE_ENABLED", "0")
    monkeypatch.delenv("JAMENDO_CLIENT_ID", raising=False)
    monkeypatch.delenv("JAMENDO_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return {"asset_root": asset_root, "source_root": source_root}
