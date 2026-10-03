"""The local phase of a call that takes local files (preparation and upload), run in the background.

A host call waits for its local phase at most CASSETTE_LOCAL_WAIT_SEC. When the phase is not done
by then, the call returns the non-error `preparing` result and the work goes on; calling again with
the same arguments attaches to the same task (contract §7). A finished phase's refs stay cached for
the life of the bridge process; a failed one is reported once and then forgotten, so the next call
starts over.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from anyio.abc import TaskGroup, TaskStatus

from oh_my_cassette.bridge.errors import BridgeError
from oh_my_cassette.bridge.upstream import describe

log = logging.getLogger(__name__)

TRANSCODING, UPLOADING, DONE = "transcoding", "uploading", "done"
PROGRESS_INTERVAL_SEC = 1.0
HEARTBEAT_SEC = 15.0

ReportFn = Callable[[float, float | None, str | None], Awaitable[None]]


@dataclass
class FileProgress:
    """One local file's place in its local phase. `stages` are the steps it goes through."""

    path: str
    stages: tuple[str, ...]
    stage: str = field(init=False)
    percent: float = 0.0

    def __post_init__(self) -> None:
        self.stage = self.stages[0]

    def update(self, stage: str, percent: float = 0.0) -> None:
        self.stage, self.percent = stage, max(0.0, min(100.0, percent))

    def fraction(self) -> float:
        if self.stage == DONE:
            return 1.0
        return (self.stages.index(self.stage) + self.percent / 100) / len(self.stages)

    def wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "stage": self.stage,
            "percent": round(100.0 if self.stage == DONE else self.percent, 1),
        }


class LocalJob:
    def __init__(self, files: list[FileProgress]) -> None:
        self.files = files
        self.done = anyio.Event()
        self.refs: dict[Path, str] | None = None
        self.error: BridgeError | None = None

    def progress(self) -> dict[str, Any]:
        finished = sum(1 for file in self.files if file.stage == DONE)
        return {"done": finished, "total": len(self.files), "files": [file.wire() for file in self.files]}

    def percent(self) -> float:
        return 100 * sum(file.fraction() for file in self.files) / max(1, len(self.files))

    def summary(self) -> str:
        """A short line for progress messages: how many are done, then the files still in progress."""
        open_files = [file for file in self.files if file.stage != DONE]
        shown = ", ".join(f"{file.path} {file.stage} {file.percent:.0f}%" for file in open_files[:2])
        more = f" (+{len(open_files) - 2} more)" if len(open_files) > 2 else ""
        finished = len(self.files) - len(open_files)
        return f"Preparing local files, {finished}/{len(self.files)} done" + (
            f": {shown}{more}" if shown else ""
        )


def preparing_result(tool_name: str, job: LocalJob) -> types.CallToolResult:
    """The non-error answer for a call whose local phase outlived the wait window."""
    text = (
        f"{job.summary()}. The files are still being prepared locally for {tool_name}; the work continues in "
        f"the background. Call {tool_name} again with the same arguments to keep waiting."
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content={"status": "preparing", "progress": job.progress()},
    )


class LocalJobs:
    def __init__(self, *, heartbeat_sec: float = HEARTBEAT_SEC) -> None:
        self._heartbeat_sec = heartbeat_sec
        self._jobs: dict[Hashable, LocalJob] = {}
        self._tg: TaskGroup | None = None

    async def run(self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        """Own the background tasks until cancelled (bridge shutdown cancels the work with it)."""
        async with anyio.create_task_group() as tg:
            self._tg = tg
            task_status.started()
            await anyio.sleep_forever()

    def attach(
        self,
        key: Hashable,
        files: Callable[[], list[FileProgress]],
        work: Callable[[LocalJob], Awaitable[dict[Path, str]]],
    ) -> LocalJob:
        """The job for `key`: the running or finished one, else a new one started now."""
        job = self._jobs.get(key)
        if job is None:
            if self._tg is None:
                raise RuntimeError("LocalJobs.run is not running")
            job = self._jobs[key] = LocalJob(files())
            self._tg.start_soon(self._execute, job, work)
        return job

    async def _execute(self, job: LocalJob, work: Callable[[LocalJob], Awaitable[dict[Path, str]]]) -> None:
        try:
            job.refs = await work(job)
        except BridgeError as exc:
            job.error = exc
        except Exception as exc:
            log.exception("local preparation failed")
            job.error = BridgeError(
                "bridge.upload_failed", f"local phase failed: {describe(exc)}", retryable=True
            )
        finally:
            job.done.set()

    async def wait(self, job: LocalJob, timeout: float, report: ReportFn) -> bool:
        """Wait up to `timeout` seconds, forwarding progress; True when the job has finished."""
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._report, job, report)
            with anyio.move_on_after(max(0.0, timeout)):
                await job.done.wait()
            tg.cancel_scope.cancel()
        return job.done.is_set()

    def settle(self, key: Hashable, job: LocalJob) -> dict[Path, str]:
        """A finished job's refs, or its error, raised once: a failed job is forgotten here."""
        if job.error is not None:
            if self._jobs.get(key) is job:
                del self._jobs[key]
            raise job.error
        assert job.refs is not None
        return job.refs

    async def _report(self, job: LocalJob, report: ReportFn) -> None:
        """At most one notification a second, and one at least every heartbeat even without news.

        The value is the overall percent, nudged up when nothing moved so it always increases.
        """
        last_value, last_sent, last_message = -math.inf, -math.inf, None
        while not job.done.is_set():
            now, message = anyio.current_time(), job.summary()
            if message != last_message or now - last_sent >= self._heartbeat_sec:
                value = max(job.percent(), last_value + 0.01)
                try:
                    await report(value, 100.0, message)
                except Exception:  # a host that stopped listening must not end the wait
                    log.debug("progress notification failed", exc_info=True)
                last_value, last_sent, last_message = value, now, message
            await anyio.sleep(PROGRESS_INTERVAL_SEC)
