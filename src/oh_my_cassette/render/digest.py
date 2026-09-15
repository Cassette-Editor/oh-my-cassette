"""Bounded, human-readable views of a TimelineDocument and of the change between two versions.

The digest is what the host model reads after every turn, so it must stay short (a few dozen
lines) no matter how big the project grows. It never invents structure: everything comes from
``document.entities`` and the active sequence's ordering arrays.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MAX_CLIPS_PER_TRACK = 12
MAX_LINES = 40


def _frames_to_sec(frames: int, timebase: dict[str, Any]) -> float:
    num, den = int(timebase.get("num", 30)), int(timebase.get("den", 1))
    fps = num / den if den else 30
    return frames / fps if fps else 0.0


def _fmt_time(sec: float) -> str:
    if sec < 60:
        return f"{sec:.2f}s"
    minutes, rest = divmod(sec, 60)
    return f"{int(minutes)}m{rest:04.1f}s"


def active_sequence(document: dict[str, Any]) -> dict[str, Any] | None:
    sequences = document.get("entities", {}).get("sequences", {})
    return sequences.get(document.get("activeSequenceId"))


def sequence_arrays(
    document: dict[str, Any], sequence_id: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    entities = document.get("entities", {})
    seq = entities.get("sequences", {}).get(sequence_id or document.get("activeSequenceId"))
    if not seq:
        return {"tracks": [], "clips": [], "transitions": []}
    tracks = [
        entities.get("tracks", {})[tid]
        for tid in seq.get("trackIds", [])
        if tid in entities.get("tracks", {})
    ]
    clips = [
        entities.get("clips", {})[cid] for cid in seq.get("clipIds", []) if cid in entities.get("clips", {})
    ]
    transitions = [
        entities.get("transitions", {})[xid]
        for xid in seq.get("transitionIds", [])
        if xid in entities.get("transitions", {})
    ]
    return {"tracks": tracks, "clips": clips, "transitions": transitions}


def total_frames(clips: list[dict[str, Any]]) -> int:
    end = 0
    for clip in clips:
        end = max(end, int(clip.get("startFrame", 0)) + int(clip.get("durationInFrames", 0)))
    return end


def clip_label(clip: dict[str, Any], media_names: dict[str, str] | None = None) -> str:
    kind = clip.get("type", "?")
    source = clip.get("source") or {}
    if kind == "text":
        text = str(source.get("text") or source.get("content") or "").strip().replace("\n", " ")
        return f'text "{text[:40]}"' if text else "text"
    if source.get("kind") == "media-file":
        media_id = source.get("mediaFileId", "")
        name = (media_names or {}).get(media_id) or clip.get("displayName") or media_id[:8]
        return f"{kind} {name}"
    return f"{kind} {clip.get('displayName', '')}".strip()


def timeline_digest(document: dict[str, Any], media_names: dict[str, str] | None = None) -> str:
    """Return a bounded multi-line summary of the active sequence."""
    seq = active_sequence(document)
    if not seq:
        return "empty document (no active sequence)"
    arrays = sequence_arrays(document)
    timebase = seq.get("timebase") or document.get("settings", {}).get(
        "defaultTimebase", {"num": 30, "den": 1}
    )
    frames = total_frames(arrays["clips"])
    lines = [
        f"v{document.get('version', 0)} · {seq.get('displayName', seq.get('id'))} · "
        f"{seq.get('compositionWidth')}x{seq.get('compositionHeight')} @ {timebase.get('num')}/{timebase.get('den')} fps · "
        f"{_fmt_time(_frames_to_sec(frames, timebase))} ({frames} frames) · "
        f"{len(arrays['tracks'])} tracks · {len(arrays['clips'])} clips · {len(arrays['transitions'])} transitions"
    ]
    by_track: dict[str, list[dict[str, Any]]] = {}
    for clip in arrays["clips"]:
        by_track.setdefault(clip.get("trackId", "?"), []).append(clip)
    for track in arrays["tracks"]:
        clips = sorted(by_track.get(track["id"], []), key=lambda c: int(c.get("startFrame", 0)))
        flags = []
        if track.get("muted"):
            flags.append("muted")
        if track.get("locked"):
            flags.append("locked")
        if track.get("visible") is False:
            flags.append("hidden")
        if track.get("isMain"):
            flags.append("main")
        head = f"[{track['id']}] {track.get('displayName', '')} ({track.get('type')}{', ' + ', '.join(flags) if flags else ''}) · {len(clips)} clips"
        lines.append(head)
        for clip in clips[:MAX_CLIPS_PER_TRACK]:
            start = int(clip.get("startFrame", 0))
            dur = int(clip.get("durationInFrames", 0))
            window = clip.get("sourceWindow") or {}
            src = ""
            if window:
                src = f" src {window.get('startUs', 0) / 1e6:.2f}s→{window.get('endUs', 0) / 1e6:.2f}s"
            disabled = " (disabled)" if clip.get("disabled") else ""
            lines.append(
                f"  {clip.get('id')}: {clip_label(clip, media_names)} @ {_fmt_time(_frames_to_sec(start, timebase))}"
                f"→{_fmt_time(_frames_to_sec(start + dur, timebase))}{src}{disabled}"
            )
        if len(clips) > MAX_CLIPS_PER_TRACK:
            lines.append(f"  … {len(clips) - MAX_CLIPS_PER_TRACK} more clips")
        if len(lines) >= MAX_LINES:
            lines.append("… (digest truncated)")
            break
    return "\n".join(lines)


@dataclass
class TimelineDelta:
    version_from: int
    version_to: int
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    tracks_added: list[str] = field(default_factory=list)
    tracks_removed: list[str] = field(default_factory=list)
    duration_from_frames: int = 0
    duration_to_frames: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.tracks_added or self.tracks_removed)

    def summary(self, before: dict[str, Any] | None = None, after: dict[str, Any] | None = None) -> str:
        parts = []
        names_after = (
            {c["id"]: clip_label(c) for c in (after or {}).get("entities", {}).get("clips", {}).values()}
            if after
            else {}
        )
        names_before = (
            {c["id"]: clip_label(c) for c in (before or {}).get("entities", {}).get("clips", {}).values()}
            if before
            else {}
        )
        if self.added:
            parts.append(
                "added "
                + ", ".join(f"{i} ({names_after.get(i, '?')})" for i in self.added[:8])
                + (" …" if len(self.added) > 8 else "")
            )
        if self.removed:
            parts.append(
                "removed "
                + ", ".join(f"{i} ({names_before.get(i, '?')})" for i in self.removed[:8])
                + (" …" if len(self.removed) > 8 else "")
            )
        if self.changed:
            parts.append("changed " + ", ".join(self.changed[:8]) + (" …" if len(self.changed) > 8 else ""))
        if self.tracks_added:
            parts.append("tracks added " + ", ".join(self.tracks_added))
        if self.tracks_removed:
            parts.append("tracks removed " + ", ".join(self.tracks_removed))
        if self.duration_from_frames != self.duration_to_frames:
            parts.append(f"duration {self.duration_from_frames}→{self.duration_to_frames} frames")
        head = f"v{self.version_from}→v{self.version_to}"
        return head + (": " + "; ".join(parts) if parts else ": no timeline change")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_from": self.version_from,
            "version_to": self.version_to,
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "tracks_added": self.tracks_added,
            "tracks_removed": self.tracks_removed,
            "duration_from_frames": self.duration_from_frames,
            "duration_to_frames": self.duration_to_frames,
        }


def timeline_delta(before: dict[str, Any], after: dict[str, Any]) -> TimelineDelta:
    b_clips = before.get("entities", {}).get("clips", {})
    a_clips = after.get("entities", {}).get("clips", {})
    b_tracks = before.get("entities", {}).get("tracks", {})
    a_tracks = after.get("entities", {}).get("tracks", {})
    delta = TimelineDelta(version_from=int(before.get("version", 0)), version_to=int(after.get("version", 0)))
    delta.added = sorted(set(a_clips) - set(b_clips))
    delta.removed = sorted(set(b_clips) - set(a_clips))
    delta.changed = sorted(
        cid
        for cid in set(a_clips) & set(b_clips)
        if json.dumps(a_clips[cid], sort_keys=True) != json.dumps(b_clips[cid], sort_keys=True)
    )
    delta.tracks_added = sorted(set(a_tracks) - set(b_tracks))
    delta.tracks_removed = sorted(set(b_tracks) - set(a_tracks))
    delta.duration_from_frames = total_frames(sequence_arrays(before)["clips"])
    delta.duration_to_frames = total_frames(sequence_arrays(after)["clips"])
    return delta
