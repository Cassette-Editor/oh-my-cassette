"""Text renderers for Cassette project timelines.

Turns the ProjectDocument JSON (GET /api/projects/{sid}, ProjectCommitEvent.document) into
bounded, phone-glanceable text:

* ``render_ctl``      — aligned fixed-width digest for monospace surfaces (TUI, envelopes)
* ``render_ctl_gateway`` — unpadded single-line-per-track profile for proportional-font chats
* ``render_delta``    — "+ ~ -" change summary between two document snapshots
* ``plan_review_block`` — edit_plan_review interrupt rendered against the current timeline

Everything is a pure function over plain dicts (no models, no HTTP) and bounded by
construction: CTL ≤ ~30 lines, delta ≤ ~15 lines. Clip letters are deterministic for a given
document (per-track timeline order).
# ponytail: letters are positional per render, not sticky across versions — deltas always carry
# the clip NAME so the letter is a hint, never the identifier; sticky letters would need state.
"""

from __future__ import annotations

import math
from typing import Any

CTL_MAX_LINES = 30
DELTA_MAX_LINES = 15
_TRACK_CLIP_BUDGET = 8
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ── time helpers ──────────────────────────────────────────────────────────────


def _timebase(document: dict) -> float:
    tb = document.get("sequenceTimebase") or {}
    num = float(tb.get("num") or document.get("fps") or 30)
    den = float(tb.get("den") or 1)
    return num / den if den else 30.0


def _seconds(frames: Any, fps: float) -> float:
    try:
        return float(frames) / fps if fps else 0.0
    except (TypeError, ValueError):
        return 0.0


def _tc(seconds: float) -> str:
    minutes, secs = divmod(max(0.0, seconds), 60)
    return f"{int(minutes):02d}:{secs:04.1f}"


def _sec_short(seconds: float) -> str:
    return f"{seconds:.1f}".rstrip("0").rstrip(".") or "0"


def _aspect(document: dict) -> str:
    w = int(document.get("compositionWidth") or 0)
    h = int(document.get("compositionHeight") or 0)
    if not w or not h:
        return ""
    g = math.gcd(w, h) or 1
    fps = _timebase(document)
    fps_txt = str(int(fps)) if float(fps).is_integer() else f"{fps:.2f}"
    return f"{w // g}:{h // g} {h}p{fps_txt}"


# ── document accessors ────────────────────────────────────────────────────────


def _clips(document: dict) -> dict[str, dict]:
    return (document.get("entities") or {}).get("clips") or {}


def _tracks_in_order(document: dict) -> list[dict]:
    entities = document.get("entities") or {}
    tracks = entities.get("tracks") or {}
    order = (document.get("order") or {}).get("trackIds") or list(tracks)
    return [tracks[tid] for tid in order if tid in tracks]


def track_labels(document: dict) -> dict[str, str]:
    """Track id -> V1/V2/A1… labels, in document track order."""
    labels: dict[str, str] = {}
    counters = {"video": 0, "audio": 0}
    for track in _tracks_in_order(document):
        kind = "audio" if track.get("type") == "audio" else "video"
        counters[kind] += 1
        labels[str(track.get("id"))] = f"{'A' if kind == 'audio' else 'V'}{counters[kind]}"
    return labels


def _track_clips(document: dict, track_id: str) -> list[dict]:
    clips = [c for c in _clips(document).values() if c.get("trackId") == track_id and not c.get("isGap")]
    clips.sort(key=lambda c: (float(c.get("startFrame") or 0), str(c.get("id"))))
    return clips


def clip_letters(document: dict) -> dict[str, str]:
    """Clip id -> per-track letter (A, B, … timeline order). Deterministic for a document."""
    letters: dict[str, str] = {}
    for track in _tracks_in_order(document):
        for index, clip in enumerate(_track_clips(document, str(track.get("id")))):
            letters[str(clip.get("id"))] = _LETTERS[index % len(_LETTERS)] * (1 + index // len(_LETTERS))
    return letters


def total_duration_seconds(document: dict) -> float:
    fps = _timebase(document)
    end = 0.0
    for clip in _clips(document).values():
        end = max(end, _seconds(clip.get("startFrame"), fps) + _seconds(clip.get("durationInFrames"), fps))
    return end


def _clip_display_name(clip: dict) -> str:
    if clip.get("type") == "text":
        text = str(clip.get("text") or clip.get("name") or "").strip().replace("\n", " ")
        return f'"{text[:24]}"' if text else '"text"'
    return str(clip.get("sourceDisplayName") or clip.get("name") or clip.get("id") or "clip")


def _clip_suffixes(clip: dict, fps: float) -> list[str]:
    out: list[str] = []
    speed = clip.get("speed")
    if isinstance(speed, (int, float)) and speed not in (0, 1):
        out.append(f"speed {speed:g}x")
    volume = clip.get("volume")
    if isinstance(volume, (int, float)) and volume != 1:
        out.append(f"vol {volume:g}")
    if clip.get("fadeInFrames"):
        out.append(f"fade-in {_sec_short(_seconds(clip.get('fadeInFrames'), fps))}s")
    if clip.get("fadeOutFrames"):
        out.append(f"fade-out {_sec_short(_seconds(clip.get('fadeOutFrames'), fps))}s")
    if clip.get("reversed"):
        out.append("reversed")
    if clip.get("disabled"):
        out.append("disabled")
    return out


def _clip_range(clip: dict, fps: float) -> tuple[float, float]:
    start = _seconds(clip.get("startFrame"), fps)
    return start, start + _seconds(clip.get("durationInFrames"), fps)


# ── CTL (aligned profile) ─────────────────────────────────────────────────────


def _ctl_clip_line(prefix: str, letter: str, clip: dict, fps: float) -> str:
    start, end = _clip_range(clip, fps)
    parts = [f"[{letter} {_clip_display_name(clip)}  {_tc(start)}-{_tc(end)}"]
    suffixes = _clip_suffixes(clip, fps)
    if suffixes:
        parts.append("  " + "  ".join(suffixes))
    return f"{prefix}{''.join(parts)}]"


def render_ctl(document: dict, detail: str | None = None) -> str:
    """Aligned CTL digest, hard-capped at CTL_MAX_LINES. ``detail='track:V1'`` expands one track."""
    fps = _timebase(document)
    labels = track_labels(document)
    letters = clip_letters(document)
    expand = ""
    if detail and detail.startswith("track:"):
        expand = detail.split(":", 1)[1].strip()

    header = " ".join(
        part
        for part in (
            f"TIMELINE {document.get('projectId') or ''}".strip(),
            f"v{document.get('version', 0)}",
            _aspect(document),
            _tc(total_duration_seconds(document)),
        )
        if part
    )
    lines = [header]

    tracks = _tracks_in_order(document)
    # Reserve room for transitions (up to 3 lines) inside the cap.
    budget = CTL_MAX_LINES - 1

    transition_lines: list[str] = []
    transitions = (document.get("entities") or {}).get("transitions") or {}
    clips = _clips(document)
    for tr in list(transitions.values())[:3]:
        frm = letters.get(str(tr.get("fromClipId")), "?")
        to = letters.get(str(tr.get("toClipId")), "?")
        cut = _tc(_seconds(tr.get("cutFrame"), fps))
        name = str(tr.get("descriptorId") or tr.get("kind") or "transition")
        if str(tr.get("fromClipId")) in clips or str(tr.get("toClipId")) in clips:
            transition_lines.append(f"FX | {name} {frm}->{to} @{cut}")
    budget -= len(transition_lines)

    for track in tracks:
        track_id = str(track.get("id"))
        label = labels.get(track_id, "?")
        clips_in_track = _track_clips(document, track_id)
        if not clips_in_track:
            continue
        prefix = f"{label:<2} | "
        cont = "   | "
        expanded = expand and expand.lower() == label.lower()
        remaining_tracks_need = sum(1 for t in tracks if _track_clips(document, str(t.get("id"))))
        if not expanded and (len(clips_in_track) > _TRACK_CLIP_BUDGET or budget - len(clips_in_track) < remaining_tracks_need):
            if budget <= remaining_tracks_need or (len(clips_in_track) > _TRACK_CLIP_BUDGET and budget < 6):
                start, _ = _clip_range(clips_in_track[0], fps)
                _, end = _clip_range(clips_in_track[-1], fps)
                lines.append(f"{prefix}{len(clips_in_track)} clips  {_tc(start)}-{_tc(end)}  (detail=track:{label})")
                budget -= 1
                continue
            head, tail = clips_in_track[:3], clips_in_track[-2:]
            middle = clips_in_track[3:-2]
            for index, clip in enumerate(head):
                lines.append(_ctl_clip_line(prefix if index == 0 else cont, letters[str(clip["id"])], clip, fps))
            if middle:
                m_start, _ = _clip_range(middle[0], fps)
                _, m_end = _clip_range(middle[-1], fps)
                lines.append(f"{cont}... +{len(middle)} clips {_tc(m_start)}-{_tc(m_end)} ...  (detail=track:{label})")
            for clip in tail:
                lines.append(_ctl_clip_line(cont, letters[str(clip["id"])], clip, fps))
            budget -= min(len(clips_in_track), 6)
            continue
        for index, clip in enumerate(clips_in_track):
            lines.append(_ctl_clip_line(prefix if index == 0 else cont, letters[str(clip["id"])], clip, fps))
        budget -= len(clips_in_track)

    lines.extend(transition_lines)
    return "\n".join(lines[:CTL_MAX_LINES])


# ── CTL (gateway profile) ─────────────────────────────────────────────────────


def render_ctl_gateway(document: dict) -> str:
    """Unpadded profile for proportional-font chats: one short line per track, arrows between clips."""
    fps = _timebase(document)
    labels = track_labels(document)
    duration = _tc(total_duration_seconds(document))
    aspect = _aspect(document).split(" ")[0] if _aspect(document) else ""
    header = " · ".join(p for p in (f"TIMELINE v{document.get('version', 0)}", duration, aspect) if p)
    lines = [header]
    for track in _tracks_in_order(document):
        track_id = str(track.get("id"))
        clips_in_track = _track_clips(document, track_id)
        if not clips_in_track:
            continue
        tokens = []
        for clip in clips_in_track[:6]:
            start, end = _clip_range(clip, fps)
            token = f"{_clip_display_name(clip)} {_sec_short(start)}–{_sec_short(end)}"
            extras = _clip_suffixes(clip, fps)
            if extras:
                token += f" ({', '.join(extras)})"
            tokens.append(token)
        if len(clips_in_track) > 6:
            tokens.append(f"+{len(clips_in_track) - 6} more")
        lines.append(f"{labels.get(track_id, '?')}: " + " → ".join(tokens))
    return "\n".join(lines[:CTL_MAX_LINES])


# ── delta between two document snapshots ──────────────────────────────────────

_SALIENT_FIELDS = (
    "startFrame",
    "durationInFrames",
    "inSec",
    "speed",
    "text",
    "volume",
    "trackId",
    "fadeInFrames",
    "fadeOutFrames",
    "reversed",
    "disabled",
)


def _describe_change(clip_before: dict, clip_after: dict, fps: float) -> str:
    changes: list[str] = []
    for field in _SALIENT_FIELDS:
        old, new = clip_before.get(field), clip_after.get(field)
        if old == new:
            continue
        if field in {"startFrame", "durationInFrames", "fadeInFrames", "fadeOutFrames"}:
            changes.append(f"{field.removesuffix('InFrames').removesuffix('Frame')} {_tc(_seconds(old or 0, fps))} -> {_tc(_seconds(new or 0, fps))}")
        elif field == "text":
            changes.append(f'text -> "{str(new or "")[:24]}"')
        else:
            changes.append(f"{field} {old!r} -> {new!r}")
    return ", ".join(changes[:3]) if changes else "properties changed"


def render_delta(before: dict, after: dict) -> str:
    """"+ ~ -" summary between two ProjectDocument snapshots, capped at DELTA_MAX_LINES."""
    fps = _timebase(after) or _timebase(before)
    labels = track_labels(after)
    letters = clip_letters(after)
    before_clips, after_clips = _clips(before), _clips(after)

    def _label(clip: dict, fallback_doc: dict) -> str:
        track = labels.get(str(clip.get("trackId"))) or track_labels(fallback_doc).get(str(clip.get("trackId")), "?")
        letter = letters.get(str(clip.get("id")))
        return f"{track}/{letter}" if letter else track or "?"

    added = [c for cid, c in after_clips.items() if cid not in before_clips and not c.get("isGap")]
    removed = [c for cid, c in before_clips.items() if cid not in after_clips and not c.get("isGap")]
    changed = [
        (before_clips[cid], c)
        for cid, c in after_clips.items()
        if cid in before_clips and before_clips[cid] != c and not c.get("isGap")
    ]

    ops = len(added) + len(removed) + len(changed)
    header = f"CHANGES v{before.get('version', 0)} -> v{after.get('version', 0)}  ({ops} change{'s' if ops != 1 else ''})"
    lines = [header]
    for old, new in changed:
        lines.append(f"~ {_label(new, after)} {_clip_display_name(new)}  {_describe_change(old, new, fps)}")
    for clip in added:
        start, end = _clip_range(clip, fps)
        lines.append(f"+ {_label(clip, after)} {_clip_display_name(clip)}  {_tc(start)}-{_tc(end)}")
    for clip in removed:
        lines.append(f"- {_label(clip, before)} {_clip_display_name(clip)}")

    if len(lines) > DELTA_MAX_LINES:
        hidden = len(lines) - DELTA_MAX_LINES + 1
        lines = lines[: DELTA_MAX_LINES - 1] + [f"… +{hidden} more changes"]

    before_total, after_total = total_duration_seconds(before), total_duration_seconds(after)
    if abs(before_total - after_total) >= 0.05:
        lines.append(f"total {_tc(before_total)} -> {_tc(after_total)}")
    return "\n".join(lines) if ops else f"CHANGES v{before.get('version', 0)} -> v{after.get('version', 0)}  (no clip changes)"


# ── plan review block ─────────────────────────────────────────────────────────


def plan_review_block(payload: dict, document: dict | None = None, *, max_chars: int = 700) -> str:
    """Render an edit_plan_review interrupt payload for a needs_user question."""
    lines = ["PLAN REVIEW  (approve / revise <feedback> / reject)"]
    if document:
        clips = [c for c in _clips(document).values() if not c.get("isGap")]
        lines.append(
            f"Current: {len(clips)} clip{'s' if len(clips) != 1 else ''}, "
            f"v{document.get('version', 0)}, {_tc(total_duration_seconds(document))}"
        )
    review = str(payload.get("reviewMarkdown") or payload.get("summary") or "").strip()
    if review:
        lines.append(review)
    moments = payload.get("generativeMoments")
    if isinstance(moments, list) and moments:
        lines.append(f"({len(moments)} generative moment{'s' if len(moments) != 1 else ''} — approve accepts all)")
    text = "\n".join(lines)
    return text[: max_chars - 1] + "…" if len(text) > max_chars else text
