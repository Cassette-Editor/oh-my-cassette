"""Helpers for driving the bridge in-process from a host-side MCP client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
from mcp.client import Client

from oh_my_cassette.bridge.errors import BridgeError
from oh_my_cassette.bridge.prepare import Artifact, Preparer
from oh_my_cassette.bridge.server import Bridge
from oh_my_cassette.bridge.settings import BridgeSettings


@asynccontextmanager
async def bridge_host(
    settings: BridgeSettings, *, mode: str = "auto", message_handler=None, preparer: Preparer | None = None
) -> AsyncIterator[tuple[Bridge, Client]]:
    """A bridge connected upstream, plus an in-process host client talking to it."""
    async with httpx.AsyncClient() as http, anyio.create_task_group() as tg:
        bridge = Bridge(settings, http, preparer=preparer)
        await tg.start(bridge.run)
        async with Client(bridge.build_server(), mode=mode, message_handler=message_handler) as host:
            yield bridge, host
        tg.cancel_scope.cancel()


class FakePreparer:
    """Stands in for ffmpeg: writes small marker files, counts its calls, and can be slow or fail."""

    def __init__(self, *, delay: float = 0.0, failures: int = 0) -> None:
        self.delay = delay
        self.failures = failures
        self.transcodes = 0
        self.conversions = 0

    async def _work(self, progress) -> None:
        for step in range(1, 6):
            await anyio.sleep(self.delay / 5)
            progress(step * 20.0)
        if self.failures:
            self.failures -= 1
            raise BridgeError("bridge.prepare_failed", "transcoding failed (ffmpeg exit 1): fake failure")

    async def transcode(self, source: Path, renditions, workdir: Path, progress):
        self.transcodes += 1
        await self._work(progress)
        workdir.mkdir(parents=True, exist_ok=True)
        artifacts = []
        for rendition in renditions:
            path = workdir / f"{rendition.role}.mp4"
            path.write_bytes(f"{rendition.role}:{source.name}:".encode() * 64)
            artifacts.append(Artifact(rendition.role, path, rendition.mime_type))
        return artifacts, {"fake": True, "source": source.name, "roles": [r.role for r in renditions]}

    async def convert(self, source: Path, kind: str, conversion, workdir: Path, progress):
        self.conversions += 1
        await self._work(progress)
        workdir.mkdir(parents=True, exist_ok=True)
        output = workdir / f"{source.stem}.{conversion.extension}"
        output.write_bytes(b"converted:" + source.read_bytes())
        return output
