from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from . import manifest, notifier, security
from .errors import CassetteError


_DEFAULT_SOURCES = ("netease", "qq", "kuwo", "joox")
_DEFAULT_JOOX_TOKEN = "f84ao9lMF_q7husBWRfgUw"
_DEFAULT_JOOX_BR = "4"
_VALID_SOURCES = {"netease", "qq", "kuwo", "joox"}
_SOURCE_PRIORITY = {"netease": 0, "qq": 1, "kuwo": 2, "joox": 3}
_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}
_INVALID_AUDIO_URL_VALUES = {"", "none", "null", "undefined", "false"}
_MAX_EXACT_BGM_DOWNLOAD_ATTEMPTS = 5


@dataclass
class ExactBgmConfig:
    enabled: bool = True
    sources: tuple[str, ...] = _DEFAULT_SOURCES
    netease_base_url: str = "https://api.qijieya.cn/meting/"
    qq_base_url: str = "https://tang.api.s01s.cn/music_open_api.php"
    kuwo_base_url: str = "https://kw-api.cenguigui.cn/"
    joox_base_url: str = "https://apicx.asia/api/joox_music"
    joox_token: str = _DEFAULT_JOOX_TOKEN
    joox_bitrate: str = _DEFAULT_JOOX_BR
    kuwo_level: str = "exhigh"
    search_limit: int = 10
    timeout_seconds: float = 20.0
    max_bytes: int = 60 * 1024 * 1024
    user_agent: str = "oh-my-cassette exact-bgm/0.1"
    download_dir: Path = field(default_factory=lambda: manifest.get_asset_root() / "downloads" / "exact_bgm")
    metadata_dir: Path = field(default_factory=lambda: manifest.get_asset_root() / "metadata" / "exact_bgm")

    @classmethod
    def from_env(cls) -> "ExactBgmConfig":
        enabled = _truthy(_runtime_env("CASSETTE_EXACT_BGM_ENABLED", "true"))
        sources = _source_list(_runtime_env("CASSETTE_EXACT_BGM_SOURCES", ",".join(_DEFAULT_SOURCES)))
        joox_token = _runtime_env("CASSETTE_EXACT_BGM_JOOX_TOKEN", _DEFAULT_JOOX_TOKEN)
        if not sources:
            sources = _DEFAULT_SOURCES
        return cls(
            enabled=enabled,
            sources=sources,
            netease_base_url=_runtime_env("CASSETTE_EXACT_BGM_NETEASE_BASE", "https://api.qijieya.cn/meting/"),
            qq_base_url=_runtime_env("CASSETTE_EXACT_BGM_QQ_BASE", "https://tang.api.s01s.cn/music_open_api.php"),
            kuwo_base_url=_runtime_env("CASSETTE_EXACT_BGM_KUWO_BASE", "https://kw-api.cenguigui.cn/"),
            joox_base_url=_runtime_env("CASSETTE_EXACT_BGM_JOOX_BASE", "https://apicx.asia/api/joox_music"),
            joox_token=joox_token,
            joox_bitrate=_runtime_env("CASSETTE_EXACT_BGM_JOOX_BR", _DEFAULT_JOOX_BR),
            kuwo_level=_runtime_env("CASSETTE_EXACT_BGM_KUWO_LEVEL", "exhigh"),
            search_limit=_bounded_int(_runtime_env("CASSETTE_EXACT_BGM_SEARCH_LIMIT", "10"), 1, 25, 10),
            timeout_seconds=_bounded_float(_runtime_env("CASSETTE_EXACT_BGM_TIMEOUT_SEC", "20"), 1.0, 120.0, 20.0),
            max_bytes=_bounded_int(
                _runtime_env("CASSETTE_EXACT_BGM_MAX_BYTES", str(60 * 1024 * 1024)),
                1,
                512 * 1024 * 1024,
                60 * 1024 * 1024,
            ),
            user_agent=_runtime_env("USER_AGENT", "oh-my-cassette exact-bgm/0.1"),
            download_dir=_runtime_path(
                "CASSETTE_EXACT_BGM_DOWNLOAD_DIR", manifest.get_asset_root() / "downloads" / "exact_bgm"
            ),
            metadata_dir=_runtime_path(
                "CASSETTE_EXACT_BGM_METADATA_DIR", manifest.get_asset_root() / "metadata" / "exact_bgm"
            ),
        )


@dataclass
class ExactBgmCandidate:
    provider: str
    source: str
    id: str
    title: str
    artist: str
    album: str = ""
    audio_url: str = ""
    page_url: str = ""
    cover: str = ""
    quality: str = ""
    query: str = ""
    display_index: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    source_strategies: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, *, include_raw: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not include_raw:
            data.pop("raw", None)
        return data

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source": self.source,
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "pageUrl": self.page_url,
            "query": self.query,
            "displayIndex": self.display_index,
            "sourceStrategies": self.source_strategies,
        }


class ExactBgmClient:
    def __init__(self, config: ExactBgmConfig | None = None) -> None:
        self.config = config or ExactBgmConfig.from_env()

    def search_all(self, query: str, *, limit: int | None = None) -> list[ExactBgmCandidate]:
        bounded_limit = max(1, min(int(limit or self.config.search_limit), 25))
        candidates: list[ExactBgmCandidate] = []
        errors: list[dict[str, str]] = []
        for source in self.config.sources:
            try:
                if source == "netease":
                    candidates.extend(self.search_netease(query, bounded_limit))
                elif source == "qq":
                    candidates.extend(self.search_qq(query, bounded_limit))
                elif source == "kuwo":
                    candidates.extend(self.search_kuwo(query, bounded_limit))
                elif source == "joox":
                    candidates.extend(self.search_joox(query, bounded_limit))
            except CassetteError as exc:
                errors.append({"source": source, "type": "CassetteError", "code": exc.code})
            except Exception as exc:
                errors.append({"source": source, "type": type(exc).__name__})
        if errors and not candidates:
            raise CassetteError("exact_bgm_search_failed", "All exact BGM search sources failed", {"sources": errors})
        return _dedupe_candidates(candidates)

    def search_netease(self, query: str, limit: int) -> list[ExactBgmCandidate]:
        payload = self._get_json(
            self.config.netease_base_url,
            {
                "type": "search",
                "id": query,
                "limit": limit,
                "server": "netease",
            },
        )
        if not isinstance(payload, list):
            return []
        results: list[ExactBgmCandidate] = []
        for index, item in enumerate(payload[:limit], start=1):
            if not isinstance(item, dict):
                continue
            audio_url = _normalize_audio_url(item.get("url"))
            song_id = (
                _query_param(audio_url, "id")
                or _str(item.get("id") or item.get("songid") or item.get("song_id"))
                or f"{security.safe_hash_id(query)}_{index}"
            )
            results.append(
                ExactBgmCandidate(
                    provider="musicsquare_exact",
                    source="netease",
                    id=str(song_id),
                    title=_str(item.get("name")),
                    artist=_str(item.get("artist")),
                    audio_url=audio_url,
                    cover=_str(item.get("pic")),
                    query=query,
                    display_index=index,
                    raw=item,
                    source_strategies=[{"source": "netease", "query": query, "mode": "search"}],
                )
            )
        return results

    def search_qq(self, query: str, limit: int) -> list[ExactBgmCandidate]:
        payload = self._get_json(self.config.qq_base_url, {"msg": query, "type": "json"})
        data = payload if isinstance(payload, list) else payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(data, list):
            return []
        results: list[ExactBgmCandidate] = []
        for index, item in enumerate(data[:limit], start=1):
            if not isinstance(item, dict):
                continue
            song_mid = _str(item.get("song_mid"))
            if not song_mid:
                continue
            results.append(
                ExactBgmCandidate(
                    provider="musicsquare_exact",
                    source="qq",
                    id=song_mid,
                    title=_str(item.get("song_title")),
                    artist=_str(item.get("singer_name")),
                    quality=_str(item.get("pay")),
                    query=query,
                    display_index=index,
                    raw=item,
                    source_strategies=[{"source": "qq", "query": query, "mode": "search"}],
                )
            )
        return results

    def search_kuwo(self, query: str, limit: int) -> list[ExactBgmCandidate]:
        payload = self._get_json(self.config.kuwo_base_url, {"name": query, "page": 1, "limit": limit})
        if not isinstance(payload, dict) or payload.get("code") != 200 or not isinstance(payload.get("data"), list):
            return []
        results: list[ExactBgmCandidate] = []
        for index, item in enumerate(payload["data"][:limit], start=1):
            if not isinstance(item, dict):
                continue
            rid = _str(item.get("rid"))
            if not rid:
                continue
            results.append(
                ExactBgmCandidate(
                    provider="musicsquare_exact",
                    source="kuwo",
                    id=rid,
                    title=_str(item.get("name")),
                    artist=_str(item.get("artist")),
                    album=_str(item.get("album")),
                    cover=_str(item.get("pic")),
                    query=query,
                    display_index=index,
                    raw=item,
                    source_strategies=[{"source": "kuwo", "query": query, "mode": "search"}],
                )
            )
        return results

    def search_joox(self, query: str, limit: int) -> list[ExactBgmCandidate]:
        if not self.config.joox_token:
            return []
        payload = self._get_json(
            self.config.joox_base_url,
            {
                "msg": query,
                "token": self.config.joox_token,
                "br": self.config.joox_bitrate,
            },
        )
        songs = payload.get("data", {}).get("songs") if isinstance(payload, dict) else []
        if not isinstance(songs, list):
            return []
        results: list[ExactBgmCandidate] = []
        for index, item in enumerate(songs[:limit], start=1):
            if not isinstance(item, dict):
                continue
            song_mid = _str(item.get("songmid"))
            song_id = _str(item.get("歌曲ID")) or song_mid
            if not song_id:
                continue
            results.append(
                ExactBgmCandidate(
                    provider="musicsquare_exact",
                    source="joox",
                    id=song_id,
                    title=_str(item.get("歌曲名称")),
                    artist=_str(item.get("歌手")),
                    album=_str(item.get("专辑")),
                    query=query,
                    display_index=index,
                    raw=item,
                    source_strategies=[{"source": "joox", "query": query, "mode": "search", "index": index}],
                )
            )
        return results

    def ensure_audio_url(self, candidate: ExactBgmCandidate) -> ExactBgmCandidate:
        candidate.audio_url = _normalize_audio_url(candidate.audio_url)
        if candidate.audio_url:
            return candidate
        if candidate.source == "netease":
            candidate.audio_url = _normalize_audio_url(
                _build_url(
                    self.config.netease_base_url,
                    {
                        "server": "netease",
                        "type": "url",
                        "id": candidate.id,
                    },
                )
            )
            return candidate
        if candidate.source == "qq":
            return self._load_qq_detail(candidate)
        if candidate.source == "kuwo":
            return self._load_kuwo_detail(candidate)
        if candidate.source == "joox":
            return self._load_joox_detail(candidate)
        return candidate

    def download_candidate(self, candidate: ExactBgmCandidate, output_dir: Path) -> Path:
        candidate = self.ensure_audio_url(candidate)
        candidate.audio_url = _normalize_audio_url(candidate.audio_url)
        if not candidate.audio_url:
            raise CassetteError(
                "exact_bgm_audio_url_missing", "Exact BGM candidate did not include a playable audio URL"
            )
        output_dir = _safe_output_dir(output_dir)
        extension = _extension_from_url(candidate.audio_url)
        dest = output_dir / _safe_music_filename(
            candidate.artist, candidate.title, f"{candidate.source}-{candidate.id}", extension
        )
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        part_path = dest.with_name(dest.name + ".part")
        try:
            self._download_url(candidate.audio_url, part_path, seen_urls=set())
            if not part_path.exists() or part_path.stat().st_size <= 0:
                raise CassetteError("exact_bgm_download_empty", "Exact BGM download was empty")
            os.replace(part_path, dest)
            return dest
        except Exception:
            try:
                part_path.unlink()
            except OSError:
                pass
            raise

    def _load_qq_detail(self, candidate: ExactBgmCandidate) -> ExactBgmCandidate:
        payload = self._get_json(
            self.config.qq_base_url,
            {
                "msg": candidate.query or f"{candidate.title} {candidate.artist}".strip(),
                "type": "json",
                "mid": candidate.id,
            },
        )
        data = payload if isinstance(payload, dict) else {}
        if not data:
            return candidate
        candidate.title = _str(data.get("song_title") or data.get("song_name") or candidate.title)
        candidate.artist = _str(data.get("singer_name") or candidate.artist)
        candidate.album = _str(data.get("album_name") or data.get("album_title") or candidate.album)
        candidate.cover = _str(data.get("album_pic") or data.get("singer_pic") or candidate.cover)
        candidate.page_url = _str(data.get("song_h5_url") or candidate.page_url)
        candidate.audio_url, candidate.quality = _pick_qq_audio_url(data)
        candidate.raw = {**candidate.raw, "detail": data}
        candidate.source_strategies.append({"source": "qq", "query": candidate.query, "mode": "detail"})
        return candidate

    def _load_kuwo_detail(self, candidate: ExactBgmCandidate) -> ExactBgmCandidate:
        payload = self._get_json(
            self.config.kuwo_base_url,
            {
                "id": candidate.id,
                "type": "song",
                "level": self.config.kuwo_level,
                "format": "json",
            },
        )
        data = payload.get("data") if isinstance(payload, dict) and payload.get("code") == 200 else {}
        if not isinstance(data, dict):
            return candidate
        candidate.title = _str(data.get("name") or candidate.title)
        candidate.artist = _str(data.get("artist") or candidate.artist)
        candidate.album = _str(data.get("album") or candidate.album)
        candidate.cover = _str(data.get("pic") or candidate.cover)
        candidate.audio_url = _normalize_audio_url(data.get("url") or candidate.audio_url)
        candidate.raw = {**candidate.raw, "detail": data}
        candidate.source_strategies.append({"source": "kuwo", "query": candidate.query, "mode": "detail"})
        return candidate

    def _load_joox_detail(self, candidate: ExactBgmCandidate) -> ExactBgmCandidate:
        if not self.config.joox_token:
            return candidate
        index = candidate.display_index or 1
        payload = self._get_json(
            self.config.joox_base_url,
            {
                "msg": candidate.query or f"{candidate.title} {candidate.artist}".strip(),
                "n": index,
                "token": self.config.joox_token,
                "br": self.config.joox_bitrate,
            },
        )
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            return candidate
        candidate.title = _str(data.get("歌曲名称") or candidate.title)
        candidate.artist = _str(data.get("歌手") or candidate.artist)
        candidate.album = _str(data.get("专辑") or candidate.album)
        candidate.audio_url, candidate.quality = _pick_joox_audio_url(data.get("播放链接") or {})
        candidate.raw = {**candidate.raw, "detail": data}
        candidate.source_strategies.append(
            {"source": "joox", "query": candidate.query, "mode": "detail", "index": index}
        )
        return candidate

    def _get_json(self, base_url: str, params: dict[str, Any]) -> Any:
        url = _build_url(base_url, params)
        request = Request(url, headers={"Accept": "application/json", "User-Agent": self.config.user_agent})
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                body = response.read()
        except HTTPError as exc:
            raise CassetteError(
                "exact_bgm_http_error",
                "Exact BGM API request failed",
                {"status": exc.code, "source_url_host": urlparse(base_url).netloc},
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CassetteError(
                "exact_bgm_network_error",
                "Exact BGM API request failed",
                {"type": type(exc).__name__, "source_url_host": urlparse(base_url).netloc},
            ) from exc
        if int(status) < 200 or int(status) >= 300:
            raise CassetteError(
                "exact_bgm_http_error",
                "Exact BGM API request failed",
                {"status": int(status), "source_url_host": urlparse(base_url).netloc},
            )
        try:
            return json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise CassetteError(
                "exact_bgm_invalid_json",
                "Exact BGM API returned invalid JSON",
                {"source_url_host": urlparse(base_url).netloc},
            ) from exc

    def _download_url(self, url: str, part_path: Path, *, seen_urls: set[str]) -> None:
        raw_url = _str(url)
        url = _normalize_audio_url(raw_url)
        if not url:
            raise CassetteError(
                "exact_bgm_invalid_audio_url",
                "Exact BGM download URL was not a valid http(s) URL",
                {"url": _audio_url_log_snapshot(raw_url)},
            )
        if url in seen_urls:
            raise CassetteError("exact_bgm_download_redirect_loop", "Exact BGM download URL redirected in a loop")
        seen_urls.add(url)
        request = Request(url, headers={"User-Agent": self.config.user_agent})
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if int(status) < 200 or int(status) >= 300:
                    raise CassetteError(
                        "exact_bgm_download_http_error", "Exact BGM download failed", {"status": int(status)}
                    )
                content_type = str(response.headers.get("content-type") or "").lower()
                if "json" in content_type or ("text" in content_type and "audio" not in content_type):
                    body = response.read(min(self.config.max_bytes, 1024 * 1024) + 1)
                    resolved = _audio_url_from_text(body.decode("utf-8", errors="ignore"))
                    if resolved:
                        self._download_url(resolved, part_path, seen_urls=seen_urls)
                        return
                    raise CassetteError(
                        "exact_bgm_download_unexpected_content_type", "Exact BGM download returned a non-audio response"
                    )
                if content_type and "audio" not in content_type and "octet-stream" not in content_type:
                    raise CassetteError(
                        "exact_bgm_download_unexpected_content_type", "Exact BGM download returned a non-audio response"
                    )
                written = 0
                with part_path.open("wb") as fh:
                    while True:
                        chunk = response.read(1024 * 128)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > self.config.max_bytes:
                            raise CassetteError(
                                "exact_bgm_download_too_large", "Exact BGM download exceeded the configured size limit"
                            )
                        fh.write(chunk)
        except HTTPError as exc:
            raise CassetteError(
                "exact_bgm_download_http_error", "Exact BGM download failed", {"status": exc.code}
            ) from exc
        except ValueError as exc:
            raise CassetteError(
                "exact_bgm_invalid_audio_url",
                "Exact BGM download URL was not a valid http(s) URL",
                {"type": type(exc).__name__, "message": str(exc), "url": _audio_url_log_snapshot(raw_url)},
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CassetteError(
                "exact_bgm_download_failed", "Exact BGM download failed", {"type": type(exc).__name__}
            ) from exc


def match_exact_bgm(
    *,
    session_id: str,
    instruction: str,
    title: str,
    artist: str = "",
    download: bool = True,
    config: ExactBgmConfig | None = None,
    client: ExactBgmClient | None = None,
) -> dict[str, Any]:
    cfg = config or ExactBgmConfig.from_env()
    if not cfg.enabled:
        raise CassetteError("exact_bgm_disabled", "Exact song BGM matching is disabled")
    if not title.strip():
        raise CassetteError("missing_required_arg", "title is required")
    active_client = client or ExactBgmClient(cfg)
    candidates, selected, attempts = search_exact_song(
        active_client,
        title=title,
        artist=artist,
        limit=cfg.search_limit,
    )
    if not download:
        return {
            "status": "matched",
            "provider": "musicsquare_exact",
            "title": selected.title,
            "artist": selected.artist,
            "query": selected.query,
            "source": selected.source,
            "track_id": selected.id,
            "candidateCount": len(candidates),
            "eligibleCandidates": [item.to_dict(include_raw=False) for item in candidates],
            "attempts": attempts,
        }
    selected_at = _now_iso()
    local_file = None
    download_failures: list[dict[str, Any]] = []
    download_candidates = _ordered_download_candidates(
        selected,
        candidates,
        title=title,
        artist=artist,
    )
    for candidate in download_candidates[:_MAX_EXACT_BGM_DOWNLOAD_ATTEMPTS]:
        try:
            local_file = active_client.download_candidate(candidate, cfg.download_dir)
            selected = candidate
            break
        except CassetteError as exc:
            if not _is_retryable_download_error(exc):
                raise
            failure = _candidate_failure_snapshot(candidate, exc)
            download_failures.append(failure)
            _record_candidate_failure(attempts, candidate, failure)
    if local_file is None:
        raise CassetteError(
            "exact_bgm_download_failed",
            "Exact BGM candidates could not be downloaded",
            {
                "attempts": attempts,
                "download_failures": download_failures,
                "download_attempt_limit": _MAX_EXACT_BGM_DOWNLOAD_ATTEMPTS,
            },
        )
    manifest_asset = manifest.ingest_internal_asset(
        str(local_file),
        session_id=session_id,
        original_name=_safe_music_filename(
            selected.artist, selected.title, f"{selected.source}-{selected.id}", local_file.suffix
        ),
        media_type="audio",
        caption=f"Smart BGM matched by exact song search: {selected.artist} - {selected.title}.",
        metadata={
            "source": "musicsquare_exact",
            "provider": "musicsquare_exact",
            "track_id": selected.id,
            "music_source": selected.source,
            "artist": selected.artist,
            "title": selected.title,
            "query": selected.query,
        },
    )
    metadata_path = save_exact_bgm_metadata(
        selected=selected,
        selected_at=selected_at,
        instruction=instruction,
        requested_title=title,
        requested_artist=artist,
        candidate_pool=candidates,
        attempts=attempts,
        local_file=local_file,
        metadata_dir=cfg.metadata_dir,
        manifest_asset=manifest_asset,
    )
    return {
        "status": "downloaded",
        "provider": "musicsquare_exact",
        "track_id": selected.id,
        "source": selected.source,
        "artist": selected.artist,
        "title": selected.title,
        "query": selected.query,
        "file_path": _display_path(local_file),
        "metadata_path": _display_path(metadata_path),
        "candidateCount": len(candidates),
        "selectedTrack": selected.to_dict(include_raw=False),
        "candidatePoolSnapshot": [item.snapshot() for item in candidates],
        "attempts": attempts,
        "downloadFailures": download_failures,
        "manifest_asset": _scrub_manifest_asset(manifest_asset),
    }


def search_exact_song(
    client: ExactBgmClient,
    *,
    title: str,
    artist: str = "",
    limit: int = 10,
) -> tuple[list[ExactBgmCandidate], ExactBgmCandidate, list[dict[str, Any]]]:
    clean_title = title.strip()
    clean_artist = artist.strip()
    attempts: list[dict[str, Any]] = []
    all_eligible: list[ExactBgmCandidate] = []
    search_modes = [("title_artist", f"{clean_title} {clean_artist}".strip(), True)]
    if clean_artist:
        search_modes.append(("title_only", clean_title, False))
    for mode, query, require_artist in search_modes:
        candidates = client.search_all(query, limit=limit)
        eligible = filter_exact_candidates(
            candidates,
            title=clean_title,
            artist=clean_artist,
            require_artist=require_artist,
            strict_title=bool(clean_title),
        )
        attempts.append(
            {
                "mode": mode,
                "query": query,
                "candidate_count": len(candidates),
                "eligible_count": len(eligible),
                "strict_title": bool(clean_title),
            }
        )
        all_eligible.extend(eligible)
        for candidate in _sort_exact_candidates(
            eligible, title=clean_title, artist=clean_artist, require_artist=require_artist
        ):
            candidate.query = query
            try:
                candidate = client.ensure_audio_url(candidate)
            except CassetteError as exc:
                _record_candidate_failure(attempts, candidate, _candidate_failure_snapshot(candidate, exc))
                continue
            raw_audio_url = candidate.audio_url
            candidate.audio_url = _normalize_audio_url(candidate.audio_url)
            if candidate.audio_url:
                return _dedupe_candidates(all_eligible), candidate, attempts
            failure = _candidate_failure_snapshot(
                candidate,
                CassetteError(
                    "exact_bgm_audio_url_missing", "Exact BGM candidate did not include a playable audio URL"
                ),
                audio_url=raw_audio_url,
            )
            _record_candidate_failure(attempts, candidate, failure)
        if candidates and eligible:
            attempts[-1]["downloadable_count"] = 0
    raise CassetteError(
        "exact_bgm_no_search_results",
        "Exact BGM search returned no eligible song for the requested title/artist",
        {"attempts": attempts},
    )


def filter_exact_candidates(
    candidates: list[ExactBgmCandidate],
    *,
    title: str,
    artist: str = "",
    require_artist: bool = True,
    strict_title: bool = False,
) -> list[ExactBgmCandidate]:
    result: list[ExactBgmCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.id or not candidate.title:
            continue
        if not _title_matches(title, candidate.title, strict=strict_title):
            continue
        if require_artist and artist and not _artist_matches(artist, candidate.artist):
            continue
        key = f"{candidate.source}:{candidate.id}"
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def save_exact_bgm_metadata(
    *,
    selected: ExactBgmCandidate,
    selected_at: str,
    instruction: str,
    requested_title: str,
    requested_artist: str,
    candidate_pool: list[ExactBgmCandidate],
    attempts: list[dict[str, Any]],
    local_file: Path,
    metadata_dir: Path,
    manifest_asset: dict[str, Any] | None = None,
) -> Path:
    metadata_dir = _safe_output_dir(metadata_dir)
    timestamp = selected_at.replace(":", "").replace("-", "")
    path = metadata_dir / f"{selected.source}_{_safe_stem(selected.id)[:40]}_{timestamp}.json"
    payload = {
        "selectedAt": selected_at,
        "provider": "musicsquare_exact",
        "requestedTitle": requested_title,
        "requestedArtist": requested_artist,
        "instruction": instruction,
        "selectedTrack": selected.to_dict(include_raw=False),
        "selectedTrackRaw": selected.raw,
        "candidateCount": len(candidate_pool),
        "candidatePoolSnapshot": [item.snapshot() for item in candidate_pool],
        "attempts": attempts,
        "localFile": _display_path(local_file),
        "manifestAsset": _scrub_manifest_asset(manifest_asset),
    }
    fd, tmp_name = tempfile.mkstemp(prefix=".exact-bgm-metadata.", suffix=".json", dir=str(metadata_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
        return path
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def exact_bgm_enabled() -> bool:
    return ExactBgmConfig.from_env().enabled


def _pick_qq_audio_url(data: dict[str, Any]) -> tuple[str, str]:
    # Prefer common compressed audio links before lossless links because Cassette uploads
    # are more predictable with MP3/M4A-like assets than FLAC-sized downloads.
    fields = [
        ("song_play_url_standard", "standard"),
        ("song_play_url_fq", "low"),
        ("song_play_url_hq", "hq"),
        ("song_play_url_accom", "accompaniment"),
        ("song_play_url", "default"),
        ("song_play_url_sq", "lossless"),
        ("song_play_url_pq", "lossless"),
    ]
    for key, label in fields:
        url = _normalize_audio_url(data.get(key))
        if url:
            return url, label
    return "", ""


def _pick_joox_audio_url(links: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(links, dict):
        return "", ""
    order = ["MP3 320", "AAC 192", "OGG 192", "MP3 128", "AAC 96", "AAC 48", "无损FLAC", "Hi-Res无损", "母带无损"]
    for name in order:
        url = _normalize_audio_url(links.get(name))
        if url:
            return url, name
    return "", ""


def _ordered_download_candidates(
    selected: ExactBgmCandidate,
    candidates: list[ExactBgmCandidate],
    *,
    title: str,
    artist: str = "",
) -> list[ExactBgmCandidate]:
    result = [selected]
    seen = {f"{selected.source}:{selected.id}"}
    require_artist = bool(artist.strip())
    for candidate in _sort_exact_candidates(candidates, title=title, artist=artist, require_artist=require_artist):
        key = f"{candidate.source}:{candidate.id}"
        if key in seen:
            continue
        result.append(candidate)
        seen.add(key)
    return result


def _is_retryable_download_error(exc: CassetteError) -> bool:
    return exc.code in {
        "exact_bgm_audio_url_missing",
        "exact_bgm_invalid_audio_url",
        "exact_bgm_download_empty",
        "exact_bgm_download_failed",
        "exact_bgm_download_http_error",
        "exact_bgm_download_redirect_loop",
        "exact_bgm_download_too_large",
        "exact_bgm_download_unexpected_content_type",
    }


def _record_candidate_failure(
    attempts: list[dict[str, Any]],
    candidate: ExactBgmCandidate,
    failure: dict[str, Any],
) -> None:
    for attempt in reversed(attempts):
        if attempt.get("query") == candidate.query:
            failures = attempt.setdefault("candidate_failures", [])
            if isinstance(failures, list):
                failures.append(failure)
            return
    if attempts:
        failures = attempts[-1].setdefault("candidate_failures", [])
        if isinstance(failures, list):
            failures.append(failure)


def _candidate_failure_snapshot(
    candidate: ExactBgmCandidate, exc: CassetteError, *, audio_url: Any | None = None
) -> dict[str, Any]:
    return {
        "source": candidate.source,
        "track_id": candidate.id,
        "title": candidate.title,
        "artist": candidate.artist,
        "query": candidate.query,
        "code": exc.code,
        "message": str(exc),
        "details": exc.details,
        "audio_url": _audio_url_log_snapshot(candidate.audio_url if audio_url is None else audio_url),
    }


def _sort_exact_candidates(
    candidates: list[ExactBgmCandidate],
    *,
    title: str,
    artist: str = "",
    require_artist: bool = True,
) -> list[ExactBgmCandidate]:
    return sorted(
        candidates,
        key=lambda item: (
            _match_rank(title, item.title),
            _match_rank(artist, item.artist) if require_artist and artist else 0,
            _SOURCE_PRIORITY.get(item.source, 99),
            item.display_index or 9999,
            item.id,
        ),
    )


def _dedupe_candidates(candidates: list[ExactBgmCandidate]) -> list[ExactBgmCandidate]:
    result: list[ExactBgmCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = f"{candidate.source}:{candidate.id}"
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _title_matches(target: str, candidate: str, *, strict: bool = False) -> bool:
    target_norm = _normalize_music_text(target)
    candidate_norm = _normalize_music_text(candidate)
    if not target_norm or not candidate_norm:
        return False
    if strict:
        return target_norm == candidate_norm
    return target_norm == candidate_norm or target_norm in candidate_norm or candidate_norm in target_norm


def _artist_matches(target: str, candidate: str) -> bool:
    target_norm = _normalize_music_text(target)
    candidate_norm = _normalize_music_text(candidate)
    if not target_norm:
        return True
    if not candidate_norm:
        return False
    if target_norm == candidate_norm or target_norm in candidate_norm or candidate_norm in target_norm:
        return True
    candidate_parts = [_normalize_music_text(part) for part in re.split(r"[/,，、&＋+;；|]", candidate)]
    return any(part and (target_norm == part or target_norm in part or part in target_norm) for part in candidate_parts)


def _match_rank(target: str, candidate: str) -> int:
    target_norm = _normalize_music_text(target)
    candidate_norm = _normalize_music_text(candidate)
    if target_norm == candidate_norm:
        return 0
    if target_norm in candidate_norm:
        return 1
    if candidate_norm in target_norm:
        return 2
    return 3


def _normalize_music_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower()
    text = re.sub(r"\([^)]*\)|（[^）]*）|\[[^\]]*\]|【[^】]*】", " ", text)
    text = re.sub(r"(?i)\b(feat|ft|featuring)\b", " ", text)
    text = re.sub(r"[《》〈〉「」『』“”\"']", " ", text)
    text = re.sub(r"[\s._\-~·`'\"“”‘’/\\,，、;；:：!！?？&＋+|]+", "", text)
    return text.strip()


def _runtime_env(name: str, default: str = "") -> str:
    return str(os.getenv(name, "") or notifier._runtime_env(name) or default).strip()


def _runtime_path(name: str, default: Path) -> Path:
    raw = _runtime_env(name, "")
    if not raw:
        return default
    path = Path(os.path.expandvars(raw)).expanduser()
    if not path.is_absolute():
        return manifest.get_asset_root() / path
    return path


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() not in {"0", "false", "no", "off"}


def _source_list(raw: str) -> tuple[str, ...]:
    sources: list[str] = []
    for item in re.split(r"[,;:\s]+", raw or ""):
        source = item.strip().lower()
        if source in _VALID_SOURCES and source not in sources:
            sources.append(source)
    return tuple(sources)


def _bounded_int(raw: str, minimum: int, maximum: int, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _bounded_float(raw: str, minimum: float, maximum: float, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _build_url(base_url: str, params: dict[str, Any]) -> str:
    return f"{base_url}?{urlencode(params)}"


def _query_param(raw_url: str, key: str) -> str:
    if not raw_url:
        return ""
    parsed = urlparse(raw_url)
    values = parse_qs(parsed.query).get(key) or []
    return str(values[0]) if values else ""


def _audio_url_from_text(text: str) -> str:
    stripped = (text or "").strip()
    normalized = _normalize_audio_url(stripped)
    if normalized:
        return normalized
    try:
        parsed = json.loads(stripped)
    except Exception:
        return ""
    if isinstance(parsed, str):
        return _normalize_audio_url(parsed)
    if not isinstance(parsed, dict):
        return ""
    candidates = [
        parsed.get("url"),
        parsed.get("audio"),
        parsed.get("data", {}).get("url") if isinstance(parsed.get("data"), dict) else None,
        parsed.get("data") if isinstance(parsed.get("data"), str) else None,
    ]
    for candidate in candidates:
        value = _normalize_audio_url(candidate)
        if value:
            return value
    return ""


def _normalize_audio_url(value: Any) -> str:
    raw = _str(value)
    if raw.lower() in _INVALID_AUDIO_URL_VALUES:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return raw
    return ""


def _audio_url_log_snapshot(value: Any) -> dict[str, Any]:
    raw = _str(value)
    lowered = raw.lower()
    if lowered in _INVALID_AUDIO_URL_VALUES:
        return {"status": "missing" if not raw else "invalid_literal", "value": lowered}
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return {
            "status": "valid",
            "scheme": parsed.scheme,
            "host": parsed.netloc,
            "query_keys": sorted(parse_qs(parsed.query).keys())[:10],
        }
    return {
        "status": "invalid_scheme",
        "scheme": parsed.scheme,
        "value": raw[:80],
    }


def _safe_output_dir(path: Path) -> Path:
    root = manifest.get_asset_root()
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = root / expanded
    resolved = expanded.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CassetteError(
            "exact_bgm_output_dir_outside_asset_root",
            "Exact BGM output directories must live under CASSETTE_ASSET_ROOT",
        ) from exc
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _display_path(path: Path) -> str:
    root = manifest.get_asset_root()
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name


def _extension_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in _AUDIO_EXTENSIONS:
        return suffix
    return ".mp3"


def _safe_music_filename(artist: str, title: str, track_id: str, extension: str = ".mp3") -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    if ext.lower() not in _AUDIO_EXTENSIONS:
        ext = ".mp3"
    stem = _safe_stem(f"{artist or 'BGM'} - {title or 'matched track'} - {track_id or 'unknown'}")
    return f"{stem[:160]}{ext.lower()}"


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", value)
    stem = re.sub(r"\s+", " ", stem).strip(" ._")
    return stem or "exact-bgm"


def _scrub_manifest_asset(asset: dict[str, Any] | None) -> dict[str, Any] | None:
    if not asset:
        return None
    return {
        "asset_id": asset.get("asset_id"),
        "sha256": asset.get("sha256"),
        "size_bytes": asset.get("size_bytes"),
        "session_hash": asset.get("session_hash"),
        "deduplicated": asset.get("deduplicated"),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _str(value: Any) -> str:
    return str(value or "").strip()
