"""The bridge ↔ backend contract (docs/v3/contract.md): names, parsing and JSON Pointer helpers.

Everything the bridge knows about the Cassette backend lives here. Tool names, parameters and
behavior are the backend's; only these annotations are agreed in advance.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

NAMESPACE = "io.github.cassette-editor"
EXTENSION_ID = f"{NAMESPACE}/bridge"
SUPPORTED_CONTRACT_VERSIONS: frozenset[int] = frozenset({1})

META_LOCAL_FILES = f"{NAMESPACE}/localFiles"
META_BRIDGE_ROLE = f"{NAMESPACE}/bridge"
META_DOWNLOADS = f"{NAMESPACE}/downloads"
META_WORKSPACE = f"{NAMESPACE}/workspace"
META_HOST = f"{NAMESPACE}/host"
META_ELAPSED = f"{NAMESPACE}/elapsedSeconds"

ROLE_UPLOAD_BEGIN = "upload.begin"
ROLE_UPLOAD_COMPLETE = "upload.complete"
BRIDGE_ROLES: frozenset[str] = frozenset({ROLE_UPLOAD_BEGIN, ROLE_UPLOAD_COMPLETE})

ARTIFACT_ORIGINAL = "original"
DEFAULT_LOCAL_ACCEPT: tuple[str, ...] = ("video/*", "audio/*", "image/*")
MEDIA_KINDS: tuple[str, ...] = ("video", "audio", "image")

# What this bridge can produce. A declaration outside these values is a contract violation: a backend
# that needs another encoding raises the contract version instead.
VIDEO_CODECS: frozenset[str] = frozenset({"h264"})
VIDEO_PROFILES: frozenset[str] = frozenset({"baseline", "main", "high"})
VIDEO_PRESETS: frozenset[str] = frozenset(
    {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}
)
VIDEO_CONTAINERS: frozenset[str] = frozenset({"mp4"})
AUDIO_CODECS: frozenset[str] = frozenset({"aac"})
CONVERSION_CONTAINERS: Mapping[str, frozenset[str]] = {
    "audio": frozenset({"flac"}),
    "image": frozenset({"jpeg"}),
}


class ContractError(ValueError):
    """The backend's annotations do not follow the contract."""


@dataclass(frozen=True)
class VideoEncoding:
    codec: str
    profile: str
    pixel_format: str
    bit_depth: int
    frame_rate: float
    max_long_edge: int
    max_short_edge: int
    keyframe_interval_seconds: float
    crf: int
    preset: str


@dataclass(frozen=True)
class AudioEncoding:
    codec: str
    bitrate: int
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class Rendition:
    role: str
    container: str
    mime_type: str
    video: VideoEncoding
    audio: AudioEncoding


@dataclass(frozen=True)
class Conversion:
    container: str
    mime_type: str
    extension: str
    quality: int | None = None


@dataclass(frozen=True)
class KindRule:
    """Audio or image: extensions uploaded unchanged, and what anything else is converted to."""

    accept: tuple[str, ...]
    convert: Conversion


@dataclass(frozen=True)
class PrepareSpec:
    renditions: tuple[Rendition, ...] | None
    audio: KindRule | None
    image: KindRule | None
    digest: str


@dataclass(frozen=True)
class Transcode:
    """A video becomes every declared rendition; the source itself is never uploaded."""

    renditions: tuple[Rendition, ...]


@dataclass(frozen=True)
class Convert:
    kind: str
    conversion: Conversion


@dataclass(frozen=True)
class LocalFilesSpec:
    pointers: tuple[str, ...]
    accept: tuple[str, ...]
    prepare: PrepareSpec | None = None


@dataclass(frozen=True)
class DownloadSpec:
    url: str
    file_name: str
    size: int | None = None
    sha256: str | None = None
    pointer: str | None = None


@dataclass(frozen=True)
class ServerContract:
    declared: bool
    version: int | None = None
    min_bridge_version: str | None = None

    def incompatibility(self, bridge_version: str) -> str | None:
        """Why this bridge cannot serve the backend, or None when it can."""
        if not self.declared:
            return None
        if self.version not in SUPPORTED_CONTRACT_VERSIONS:
            supported = ", ".join(str(v) for v in sorted(SUPPORTED_CONTRACT_VERSIONS))
            return f"the backend speaks bridge contract v{self.version}; this bridge supports v{supported}"
        if self.min_bridge_version and version_tuple(bridge_version) < version_tuple(self.min_bridge_version):
            return (
                f"the backend requires oh-my-cassette >= {self.min_bridge_version} (this is {bridge_version})"
            )
        return None


def version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in value.split("+", 1)[0].split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _meta_dict(meta: Any) -> Mapping[str, Any]:
    return meta if isinstance(meta, Mapping) else {}


def server_contract(extensions: Any) -> ServerContract:
    """Read the contract declaration from `capabilities.extensions`."""
    settings = _meta_dict(extensions).get(EXTENSION_ID)
    if not isinstance(settings, Mapping):
        return ServerContract(declared=False)
    version = settings.get("version")
    minimum = settings.get("minBridgeVersion")
    return ServerContract(
        declared=True,
        version=version if isinstance(version, int) else None,
        min_bridge_version=minimum if isinstance(minimum, str) else None,
    )


def bridge_role(tool_meta: Any) -> str | None:
    role = _meta_dict(tool_meta).get(META_BRIDGE_ROLE)
    return role if isinstance(role, str) else None


def local_files_spec(tool_meta: Any) -> LocalFilesSpec | None:
    raw = _meta_dict(tool_meta).get(META_LOCAL_FILES)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ContractError(f"{META_LOCAL_FILES} must be an object")
    pointers = raw.get("pointers")
    if not isinstance(pointers, list) or not pointers or not all(isinstance(p, str) for p in pointers):
        raise ContractError(f"{META_LOCAL_FILES}.pointers must be a non-empty list of JSON Pointers")
    for pointer in pointers:
        pointer_tokens(pointer)
    accept = raw.get("accept", list(DEFAULT_LOCAL_ACCEPT))
    if not isinstance(accept, list) or not all(isinstance(a, str) and "/" in a for a in accept):
        raise ContractError(f"{META_LOCAL_FILES}.accept must be a list of MIME patterns")
    prepare = raw.get("prepare")
    return LocalFilesSpec(
        pointers=tuple(pointers),
        accept=tuple(accept),
        prepare=prepare_spec(prepare) if prepare is not None else None,
    )


# ── prepare declarations (contract §3) ──


def _field(raw: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in raw:
        raise ContractError(f"{where}.{key} is required")
    return raw[key]


def _object(raw: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractError(f"{where} must be an object")
    return raw


def _text(raw: Mapping[str, Any], key: str, where: str, allowed: frozenset[str] | None = None) -> str:
    value = _field(raw, key, where)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{where}.{key} must be a non-empty string")
    if allowed is not None and value not in allowed:
        raise ContractError(
            f"{where}.{key} {value!r} is not one this bridge produces ({', '.join(sorted(allowed))})"
        )
    return value


def _integer(raw: Mapping[str, Any], key: str, where: str, low: int, high: int | None = None) -> int:
    value = _field(raw, key, where)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < low
        or (high is not None and value > high)
    ):
        bound = f"{low}..{high}" if high is not None else f">= {low}"
        raise ContractError(f"{where}.{key} must be an integer {bound}")
    return value


def _positive(raw: Mapping[str, Any], key: str, where: str) -> float:
    value = _field(raw, key, where)
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        raise ContractError(f"{where}.{key} must be a positive number")
    return float(value)


def _mime(raw: Mapping[str, Any], key: str, where: str) -> str:
    value = _text(raw, key, where)
    if "/" not in value:
        raise ContractError(f"{where}.{key} must be a MIME type")
    return value


def _rendition(raw: Any, where: str) -> Rendition:
    item = _object(raw, where)
    video = _object(_field(item, "video", where), f"{where}.video")
    audio = _object(_field(item, "audio", where), f"{where}.audio")
    vw, aw = f"{where}.video", f"{where}.audio"
    long_edge = _integer(video, "maxLongEdge", vw, 2)
    short_edge = _integer(video, "maxShortEdge", vw, 2)
    if short_edge > long_edge:
        raise ContractError(f"{vw}.maxShortEdge must not exceed maxLongEdge")
    return Rendition(
        role=_text(item, "role", where),
        container=_text(item, "container", where, VIDEO_CONTAINERS),
        mime_type=_mime(item, "mimeType", where),
        video=VideoEncoding(
            codec=_text(video, "codec", vw, VIDEO_CODECS),
            profile=_text(video, "profile", vw, VIDEO_PROFILES),
            pixel_format=_text(video, "pixelFormat", vw),
            bit_depth=_integer(video, "bitDepth", vw, 8, 8),
            frame_rate=_positive(video, "frameRate", vw),
            max_long_edge=long_edge,
            max_short_edge=short_edge,
            keyframe_interval_seconds=_positive(video, "keyframeIntervalSeconds", vw),
            crf=_integer(video, "crf", vw, 0, 51),
            preset=_text(video, "preset", vw, VIDEO_PRESETS),
        ),
        audio=AudioEncoding(
            codec=_text(audio, "codec", aw, AUDIO_CODECS),
            bitrate=_integer(audio, "bitrate", aw, 1),
            sample_rate=_integer(audio, "sampleRate", aw, 1),
            channels=_integer(audio, "channels", aw, 1, 8),
        ),
    )


def _kind_rule(raw: Any, kind: str, where: str) -> KindRule:
    rule = _object(raw, where)
    accept = _field(rule, "accept", where)
    if not isinstance(accept, list) or not all(isinstance(ext, str) and ext.strip(".") for ext in accept):
        raise ContractError(f"{where}.accept must be a list of file extensions")
    convert = _object(_field(rule, "convert", where), f"{where}.convert")
    cw = f"{where}.convert"
    extension = _text(convert, "extension", cw).lstrip(".")
    if not extension or "/" in extension or "\\" in extension:
        raise ContractError(f"{cw}.extension must be a file extension")
    quality = _integer(convert, "quality", cw, 0, 100) if "quality" in convert else None
    return KindRule(
        accept=tuple(ext.strip().lstrip(".").lower() for ext in accept),
        convert=Conversion(
            container=_text(convert, "container", cw, CONVERSION_CONTAINERS[kind]),
            mime_type=_mime(convert, "mimeType", cw),
            extension=extension.lower(),
            quality=quality,
        ),
    )


def prepare_spec(raw: Any) -> PrepareSpec:
    """Parse a `prepare` declaration. Unknown keys are ignored so the vocabulary can grow."""
    where = f"{META_LOCAL_FILES}.prepare"
    spec = _object(raw, where)
    renditions: tuple[Rendition, ...] | None = None
    if spec.get("video") is not None:
        video = _object(spec["video"], f"{where}.video")
        items = _field(video, "renditions", f"{where}.video")
        if not isinstance(items, list) or not items:
            raise ContractError(f"{where}.video.renditions must be a non-empty list")
        renditions = tuple(_rendition(item, f"{where}.video.renditions[{i}]") for i, item in enumerate(items))
        roles = [rendition.role for rendition in renditions]
        if len(set(roles)) != len(roles) or ARTIFACT_ORIGINAL in roles:
            raise ContractError(
                f"{where}.video.renditions roles must be distinct and not {ARTIFACT_ORIGINAL!r}"
            )
    audio = _kind_rule(spec["audio"], "audio", f"{where}.audio") if spec.get("audio") is not None else None
    image = _kind_rule(spec["image"], "image", f"{where}.image") if spec.get("image") is not None else None
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str)
    return PrepareSpec(
        renditions=renditions,
        audio=audio,
        image=image,
        digest=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def media_kind(mime: str) -> str | None:
    """`video`, `audio` or `image` from a MIME type's prefix; None for anything else."""
    prefix = mime.split("/", 1)[0].lower()
    return prefix if prefix in MEDIA_KINDS else None


def preparation_for(spec: PrepareSpec | None, mime: str, file_name: str) -> Transcode | Convert | None:
    """What happens to a local file before upload; None means it uploads unchanged as `original`."""
    kind = media_kind(mime)
    if spec is None or kind is None:
        return None
    if kind == "video":
        return Transcode(spec.renditions) if spec.renditions else None
    rule = spec.audio if kind == "audio" else spec.image
    if rule is None or PurePath(file_name).suffix.lstrip(".").lower() in rule.accept:
        return None
    return Convert(kind, rule.convert)


def download_specs(result_meta: Any) -> list[DownloadSpec]:
    raw = _meta_dict(result_meta).get(META_DOWNLOADS)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ContractError(f"{META_DOWNLOADS} must be a list")
    specs: list[DownloadSpec] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ContractError(f"{META_DOWNLOADS} entries must be objects")
        url, name = item.get("url"), item.get("fileName")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ContractError(f"{META_DOWNLOADS}[].url must be an http(s) URL")
        if not isinstance(name, str) or not name.strip():
            raise ContractError(f"{META_DOWNLOADS}[].fileName is required")
        size, sha, pointer = item.get("size"), item.get("sha256"), item.get("pointer")
        if pointer is not None:
            pointer_tokens(pointer)
        specs.append(
            DownloadSpec(
                url=url,
                file_name=name,
                size=size if isinstance(size, int) else None,
                sha256=sha.lower() if isinstance(sha, str) else None,
                pointer=pointer if isinstance(pointer, str) else None,
            )
        )
    return specs


def mime_accepted(mime: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(mime.lower(), pattern.lower()) for pattern in patterns)


# ── JSON Pointer (RFC 6901) ──


def pointer_tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ContractError(f"{pointer!r} is not a JSON Pointer")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def _step(node: Any, token: str) -> tuple[bool, Any]:
    if isinstance(node, Mapping):
        return (token in node, node.get(token))
    if isinstance(node, list) and token.isdigit() and int(token) < len(node):
        return (True, node[int(token)])
    return (False, None)


def pointer_get(document: Any, pointer: str) -> tuple[bool, Any]:
    """(found, value) for `pointer` in `document`."""
    node = document
    for token in pointer_tokens(pointer):
        found, node = _step(node, token)
        if not found:
            return (False, None)
    return (True, node)


def pointer_set(document: Any, pointer: str, value: Any) -> None:
    tokens = pointer_tokens(pointer)
    if not tokens:
        raise ContractError("cannot replace the whole document")
    parent = document
    for token in tokens[:-1]:
        found, parent = _step(parent, token)
        if not found:
            raise ContractError(f"{pointer} does not exist")
    last = tokens[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list) and last.isdigit() and int(last) < len(parent):
        parent[int(last)] = value
    else:
        raise ContractError(f"{pointer} does not exist")


def schema_at_pointer(schema: Any, pointer: str) -> Any:
    """The sub-schema an arguments pointer addresses (object properties and array items only)."""
    node = schema
    for token in pointer_tokens(pointer):
        if not isinstance(node, Mapping):
            return None
        if node.get("type") == "array" or "items" in node:
            node = node.get("items") if token.isdigit() else None
        else:
            node = _meta_dict(node.get("properties")).get(token)
    return node


def is_path_schema(schema: Any) -> bool:
    """True when a sub-schema is `string` or an array of `string` (a valid localFiles target)."""
    if not isinstance(schema, Mapping):
        return False
    if schema.get("type") == "string":
        return True
    items = schema.get("items")
    return schema.get("type") == "array" and isinstance(items, Mapping) and items.get("type") == "string"
