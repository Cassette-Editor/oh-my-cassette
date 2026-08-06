#!/usr/bin/env python3
"""Live ID-only Jamendo acceptance through the real stdio MCP entrypoint."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
HOSTS = ("codex", "claude", "opencode", "hermes")


class AcceptanceError(RuntimeError):
    pass


def _structured(result) -> dict:
    value = result.structuredContent
    if not isinstance(value, dict):
        raise AcceptanceError("MCP tool returned no structured result")
    if not value.get("ok"):
        error = value.get("error") or {}
        raise AcceptanceError(f"{error.get('code') or 'unknown'}: {error.get('message') or 'tool failed'}")
    return value


async def _exercise_host(host: str, client_id: str, root: Path) -> dict:
    host_root = root / host
    config_home = host_root / "config"
    data_home = host_root / "data"
    hermes_home = host_root / ".hermes"
    project = host_root / "project"
    project.mkdir(parents=True)
    environment = os.environ.copy()
    environment.pop("JAMENDO_CLIENT_ID", None)
    environment.pop("JAMENDO_TEST_CLIENT_ID", None)
    environment.update(
        {
            "CASSETTE_RUNTIME_ADAPTER": "mcp",
            "CASSETTE_MCP_HOST": host,
            "CASSETTE_PROJECT_ROOT": str(project),
            "CASSETTE_CONFIG_HOME": str(config_home),
            "CASSETTE_DATA_HOME": str(data_home),
            "HERMES_HOME": str(hermes_home),
            "CASSETTE_MCP_SKIP_BOOTSTRAP": "1",
            "CASSETTE_MCP_PYTHON": sys.executable,
        }
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "scripts" / "run_local_mcp.py")],
        cwd=str(project),
        env=environment,
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            if "cassette_jamendo_setup" not in names:
                raise AcceptanceError(f"{host} did not expose cassette_jamendo_setup")
            setup = _structured(await session.call_tool("cassette_jamendo_setup", {"client_id": client_id}))
            search = _structured(
                await session.call_tool(
                    "jamendo_music_matcher",
                    {
                        "userQuery": "calm instrumental background music",
                        "searchTerms": ["calm instrumental"],
                        "fuzzyTags": ["calm", "instrumental"],
                        "vocalInstrumental": "instrumental",
                        "download": False,
                    },
                )
            )
    serialized = json.dumps({"setup": setup, "search": search})
    if client_id in serialized:
        raise AcceptanceError(f"{host} exposed the full Client ID in MCP output")
    expected = hermes_home / ".env" if host == "hermes" else config_home / "settings.json"
    if not expected.is_file():
        raise AcceptanceError(f"{host} did not persist Jamendo configuration")
    if os.name != "nt" and stat.S_IMODE(expected.stat().st_mode) & 0o077:
        raise AcceptanceError(f"{host} Jamendo configuration is not owner-private")
    return {
        "host": host,
        "credential_source": setup["data"].get("credential_source"),
        "candidate_count": search["data"].get("candidateCount"),
        "persisted": True,
        "full_client_id_exposed": False,
    }


async def run() -> dict:
    client_id = str(os.getenv("JAMENDO_TEST_CLIENT_ID", "") or "").strip()
    if not client_id:
        raise AcceptanceError("Set JAMENDO_TEST_CLIENT_ID in the environment for live acceptance.")
    with tempfile.TemporaryDirectory(prefix="omc-jamendo-e2e-") as temporary:
        root = Path(temporary)
        results = [await _exercise_host(host, client_id, root) for host in HOSTS]
    return {"ok": True, "hosts": results}


def main() -> None:
    try:
        result = asyncio.run(run())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}))
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
