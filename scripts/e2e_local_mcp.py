#!/usr/bin/env python3
"""Maintainer live acceptance through the real local stdio MCP entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one real edit through the local Cassette MCP plugin")
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--transport", choices=("api", "browser"), default="api")
    parser.add_argument("--host", choices=("codex", "claude"), default="codex")
    parser.add_argument("--timeout-sec", type=int, default=1500)
    parser.add_argument("--model", default="DeepSeek V4 Flash")
    parser.add_argument("--thinking-level", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--language", choices=("en", "zh"), default="en")
    return parser.parse_args()


def _structured(result) -> dict:
    value = result.structuredContent
    if not isinstance(value, dict):
        raise AcceptanceError("MCP tool returned no structured result")
    if not value.get("ok"):
        error = value.get("error") or {}
        raise AcceptanceError(f"{error.get('code') or 'unknown'}: {error.get('message') or 'tool failed'}")
    return value


async def run(args: argparse.Namespace) -> dict:
    media = args.media.expanduser().resolve(strict=True)
    if not media.is_file():
        raise AcceptanceError(f"media is not a file: {media}")
    environment = os.environ.copy()
    environment.update(
        {
            "CASSETTE_RUNTIME_ADAPTER": "mcp",
            "CASSETTE_TRANSPORT": args.transport,
            "CASSETTE_MCP_HOST": args.host,
            "CASSETTE_PROJECT_ROOT": str(media.parent),
            "CASSETTE_MCP_SKIP_BOOTSTRAP": "1",
            "CASSETTE_MCP_PYTHON": sys.executable,
            "CASSETTE_MIN_BROWSER_TIMEOUT_SEC": "0",
        }
    )
    environment.setdefault("CASSETTE_CONFIG_HOME", str(Path(tempfile.mkdtemp()) / "config"))
    environment.setdefault("CASSETTE_DATA_HOME", str(Path(tempfile.mkdtemp()) / "data"))
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "scripts" / "run_local_mcp.py")],
        cwd=str(media.parent),
        env=environment,
    )
    read_timeout = timedelta(seconds=max(60, args.timeout_sec + 300))
    job_id = ""
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=read_timeout) as session:
            await session.initialize()
            ingest = _structured(await session.call_tool("cassette_ingest_media", {"source_path": str(media)}))
            session_id = ingest["session_id"]
            prompt = _structured(
                await session.call_tool(
                    "cassette_make_prompt",
                    {
                        "session_id": session_id,
                        "instruction": args.instruction,
                        "cassette_language": args.language,
                    },
                )
            )
            prompt_data = prompt["data"]
            started = _structured(
                await session.call_tool(
                    "cassette_run_job",
                    {
                        "session_id": session_id,
                        "prompt": prompt_data["prompt"],
                        "chat_message": prompt_data["chat_message"],
                        "instruction": args.instruction,
                        "cassette_model": args.model,
                        "thinking_level": args.thinking_level,
                        "cassette_language": args.language,
                        "wait": False,
                        "timeout_sec": args.timeout_sec,
                    },
                )
            )
            job_id = str(started.get("job_id") or "")
            if not job_id:
                raise AcceptanceError("cassette_run_job returned no job_id")

            deadline = time.monotonic() + args.timeout_sec
            status = started
            while status["phase"] in {"running", "exporting"} and time.monotonic() < deadline:
                wait = min(30.0, max(0.0, deadline - time.monotonic()))
                status = _structured(
                    await session.call_tool(
                        "cassette_job_status",
                        {"job_id": job_id, "wait_for_change_sec": wait},
                    )
                )

            if status["phase"] == "review_required":
                status = _structured(
                    await session.call_tool(
                        "cassette_review_completion",
                        {
                            "job_id": job_id,
                            "decision": "export",
                            "reason": "Maintainer acceptance observed a completed Cassette edit.",
                            "summary": "Live acceptance approved the completed edit for export.",
                        },
                        read_timeout_seconds=read_timeout,
                    )
                )
            if status["phase"] == "needs_user":
                raise AcceptanceError(f"live job requires user input: {job_id}")
            if status["phase"] not in {"exported", "succeeded"}:
                if status["phase"] in {"running", "exporting"}:
                    raise AcceptanceError(f"monitor budget expired while job is still running: {job_id}")
                raise AcceptanceError(f"live job ended in phase {status['phase']}: {job_id}")
            if not status.get("artifacts"):
                raise AcceptanceError(f"live job completed without a validated export artifact: {job_id}")
            artifact = status["artifacts"][0]
            path = Path(artifact["path"])
            if not path.is_file() or path.stat().st_size != artifact["size"]:
                raise AcceptanceError("validated artifact metadata does not match the exported file")
            return {
                "ok": True,
                "host": args.host,
                "transport": args.transport,
                "session_id": session_id,
                "job_id": job_id,
                "phase": status["phase"],
                "artifact": {
                    "name": artifact["name"],
                    "mime_type": artifact["mime_type"],
                    "size": artifact["size"],
                    "uri": artifact["uri"],
                },
            }


def main() -> None:
    args = parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
