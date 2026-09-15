"""Contact sheet of the active sequence, built locally with ffmpeg from the original files.

Nothing is rendered server-side: for each visual clip on the main video track we grab one frame from
the local original at the middle of the clip's source window, then tile the frames. The result is a
JPEG path plus a legend that maps tile index to clip id, so the host can describe what it sees.
"""

from __future__ import annotations

import asyncio
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any

from oh_my_cassette.cassette.errors import CassetteError, ToolNeedsFfmpeg
from oh_my_cassette.render.digest import sequence_arrays

MAX_TILES = 12
TILE_WIDTH = 320


async def build_contact_sheet(
    document: dict[str, Any],
    local_media: dict[str, str],
    dest: Path,
    *,
    ffmpeg: str = "ffmpeg",
    max_tiles: int = MAX_TILES,
) -> dict[str, Any]:
    binary = shutil.which(ffmpeg)
    if not binary:
        raise ToolNeedsFfmpeg(ffmpeg)
    arrays = sequence_arrays(document)
    main_track = next((t["id"] for t in arrays["tracks"] if t.get("isMain")), None)
    if main_track is None:
        main_track = next((t["id"] for t in arrays["tracks"] if t.get("type") == "video"), None)
    candidates = [
        c
        for c in sorted(arrays["clips"], key=lambda c: int(c.get("startFrame", 0)))
        if c.get("trackId") == main_track and c.get("type") in ("video", "image") and not c.get("disabled")
    ]
    legend: list[dict[str, Any]] = []
    skipped: list[str] = []
    frames: list[Path] = []
    tmp = Path(tempfile.mkdtemp(prefix="omc-sheet-"))
    try:
        for clip in candidates:
            if len(frames) >= max_tiles:
                break
            media_id = (clip.get("source") or {}).get("mediaFileId")
            local = local_media.get(media_id or "")
            if not local or not Path(local).exists():
                skipped.append(clip["id"])
                continue
            window = clip.get("sourceWindow") or {}
            start_us, end_us = float(window.get("startUs", 0)), float(window.get("endUs", 0))
            at_sec = (start_us + end_us) / 2e6 if end_us > start_us else 0.0
            out = tmp / f"tile_{len(frames):03d}.png"
            args = [binary, "-y", "-v", "error", "-nostdin"]
            if clip.get("type") == "video":
                args += ["-ss", f"{at_sec:.3f}"]
            args += ["-i", local, "-frames:v", "1", "-vf", f"scale={TILE_WIDTH}:-2", str(out)]
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, err = await proc.communicate()
            if proc.returncode != 0 or not out.exists():
                skipped.append(clip["id"])
                continue
            frames.append(out)
            legend.append(
                {
                    "tile": len(frames),
                    "clip_id": clip["id"],
                    "type": clip.get("type"),
                    "start_frame": clip.get("startFrame"),
                    "source_sec": round(at_sec, 3),
                }
            )
        if not frames:
            raise CassetteError(
                "contact_sheet_unavailable",
                "no visual clip with a locally available original (import files through cassette_import first)",
            )
        columns = min(4, len(frames))
        rows = math.ceil(len(frames) / columns)
        dest.parent.mkdir(parents=True, exist_ok=True)
        args = [
            binary,
            "-y",
            "-v",
            "error",
            "-nostdin",
            "-framerate",
            "1",
            "-i",
            str(tmp / "tile_%03d.png"),
            "-filter_complex",
            f"scale={TILE_WIDTH}:-2,pad={TILE_WIDTH}:ih:0:0,tile={columns}x{rows}:padding=4:margin=4",
            "-frames:v",
            "1",
            "-q:v",
            "4",
            str(dest),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise CassetteError("contact_sheet_failed", err.decode("utf-8", "replace")[-300:])
        return {
            "path": str(dest),
            "tiles": len(frames),
            "columns": columns,
            "legend": legend,
            "skipped_clip_ids": skipped,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
