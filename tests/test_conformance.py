"""`oh-my-cassette check` against the reference backend and against deliberately broken ones."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from tests.reference_remote import REFERENCE_TOKEN, ReferenceRemote, RemoteOptions, free_port, serve_remote

from oh_my_cassette.conformance import failed, run_checks

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[1]


def _by_name(checks) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for check in checks:
        grouped.setdefault(check.name, []).append(check.status)
    return grouped


async def test_reference_backend_passes_every_check(remote):
    checks = await run_checks(remote.url, token=REFERENCE_TOKEN, upload=True, arguments={"project_id": "p"})
    assert {check.status for check in checks} == {"pass"}, checks
    names = [check.name for check in checks]
    assert names[0] == "transport" and "contract" in names and names[-1] == "upload round trip"
    assert "2026-07-28" in checks[0].detail
    assert "video → canonical, preview" in next(c.detail for c in checks if c.name == "prepare")
    (begun,) = remote.begun
    assert begun["arguments"] == {"project_id": "p", "paths": ["probe"]}
    assert [a["role"] for a in begun["artifacts"]] == ["original"] and begun["preparation"] is None
    again = await run_checks(remote.url, token=REFERENCE_TOKEN, upload=True)
    assert "dedupe" in again[-1].detail  # the probe's content is already known


async def test_undeclared_contract_is_a_warning():
    remote = ReferenceRemote(options=RemoteOptions(declare_contract=False, instructions=None))
    async with serve_remote(remote):
        checks = await run_checks(remote.url, token=REFERENCE_TOKEN)
    grouped = _by_name(checks)
    assert grouped["contract"] == ["warn"] and grouped["instructions"] == ["warn"]
    assert not failed(checks)


@pytest.mark.parametrize(
    "options, name",
    [
        (RemoteOptions(bad_pointer=True), "local files"),
        (RemoteOptions(bad_prepare=True), "prepare"),
        (RemoteOptions(upload_tools=False), "upload tools"),
        (RemoteOptions(begin_without_puts=True), "upload round trip"),
        (RemoteOptions(contract_version=2), "contract"),
    ],
)
async def test_broken_backends_fail(options, name):
    remote = ReferenceRemote(options=options)
    async with serve_remote(remote):
        checks = await run_checks(remote.url, token=REFERENCE_TOKEN, upload=True)
    assert "fail" in _by_name(checks)[name], checks
    assert failed(checks)


async def test_a_handshake_era_backend_fails_the_transport_check():
    remote = ReferenceRemote(options=RemoteOptions(handshake_only=True))
    async with serve_remote(remote):
        checks = await run_checks(remote.url, token=REFERENCE_TOKEN, timeout=5)
    assert [(check.status, check.name) for check in checks] == [("fail", "transport")]
    assert "speaks only MCP 2025-11-25" in checks[0].detail and "2026-07-28" in checks[0].detail


@pytest.mark.parametrize("token, hint", [(None, "token is required"), ("wrong", "token was rejected")])
async def test_a_refused_token_fails_the_transport_check(remote, token, hint):
    checks = await run_checks(remote.url, token=token, timeout=5)
    assert [(check.status, check.name) for check in checks] == [("fail", "transport")]
    assert "HTTP 401" in checks[0].detail and hint in checks[0].detail


async def test_unreachable_endpoint_fails_fast():
    with anyio.fail_after(20):
        checks = await run_checks(f"http://127.0.0.1:{free_port()}/mcp", timeout=3)
    assert [(check.status, check.name) for check in checks] == [("fail", "transport")]


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-m", "oh_my_cassette", *args], capture_output=True, text=True, env=env, timeout=60
    )


async def test_cli_exit_codes(remote):
    ok = await anyio.to_thread.run_sync(
        _cli, "check", "--url", remote.url, "--token", REFERENCE_TOKEN, "--upload", "--json"
    )
    assert ok.returncode == 0, ok.stderr
    assert {entry["status"] for entry in json.loads(ok.stdout)} == {"pass"}

    down = await anyio.to_thread.run_sync(_cli, "check", "--url", f"http://127.0.0.1:{free_port()}/mcp")
    assert down.returncode == 1
    assert down.stdout.startswith("FAIL") and "0 passed, 0 warnings, 1 failed" in down.stdout
