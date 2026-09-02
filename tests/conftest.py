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
def _no_gateway_worker_outlives_its_test(monkeypatch):
    # A background gateway job resolves the asset root when it writes, not when it is
    # submitted. Left running, it finishes after the environment has been restored and
    # persists its record into the real ~/.hermes/cassette/jobs, where it shows up in
    # /cassette recent as a job that is running and never finishes.
    #
    # Depending on monkeypatch is what orders this correctly: pytest finalizes in reverse
    # setup order, so requesting it here forces monkeypatch to be created first and undone
    # last, leaving the sandboxed roots in place while the worker drains.
    yield
    from cassette.core import tools

    tools.shutdown_gateway_job_executor()


@pytest.fixture(autouse=True)
def _no_inherited_transport_setting(monkeypatch):
    # There is one transport, so nothing here selects it. The variable is still deleted: a
    # developer with a leftover CASSETTE_TRANSPORT=browser in their shell would otherwise
    # have every test print the retirement notice to stderr.
    monkeypatch.delenv("CASSETTE_TRANSPORT", raising=False)


@pytest.fixture
def cassette_env(tmp_path, monkeypatch):
    asset_root = tmp_path / "asset-root"
    source_root = tmp_path / "source-root"
    source_root.mkdir()
    monkeypatch.setenv("CASSETTE_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv("CASSETTE_DATA_HOME", str(tmp_path / "data-root"))
    monkeypatch.setenv("CASSETTE_ALLOWED_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("CASSETTE_ALLOWED_EXTENSIONS", ".mp4,.jpg,.png,.mp3,.txt")
    monkeypatch.setenv("CASSETTE_MAX_BYTES", "1024")
    monkeypatch.setenv("CASSETTE_MIN_JOB_TIMEOUT_SEC", "0")
    monkeypatch.setenv("CASSETTE_WEIXIN_FORCE_H264", "0")
    monkeypatch.setenv("CASSETTE_PING_ON_GATEWAY_INSTRUCTION", "0")
    monkeypatch.delenv("JAMENDO_CLIENT_ID", raising=False)
    monkeypatch.delenv("JAMENDO_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return {"asset_root": asset_root, "source_root": source_root}
