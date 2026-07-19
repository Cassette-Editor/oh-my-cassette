from __future__ import annotations

import json
import os
import random
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import manifest, notifier
from .errors import CassetteError


JAMENDO_BASE_URL = "https://api.jamendo.com/v3.0"
JAMENDO_PLAN_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "jamendo-search-plan.md"
_AUDIO_FORMAT = "mp32"
_VALID_VOCAL = {"vocal", "instrumental"}
_VALID_ACOUSTIC = {"acoustic", "electric"}
_VALID_SPEED = {"verylow", "low", "medium", "high", "veryhigh"}
_VALID_TYPES = {"single", "albumtrack"}
_NO_CONSTRAINT_VALUES = {"", "any", "all", "none", "null", "不限", "任意", "无"}
_BOOST_ALIASES = {
    "downloads": "downloads_total",
    "download": "downloads_total",
    "popular": "popularity_total",
    "popularity": "popularity_total",
    "listens": "listens_total",
    "listen": "listens_total",
}
_VALID_BOOSTS = {
    "buzzrate",
    "downloads_week",
    "downloads_month",
    "downloads_total",
    "listens_week",
    "listens_month",
    "listens_total",
    "popularity_week",
    "popularity_month",
    "popularity_total",
}
_ORDER_ALIASES = {
    "downloads": "downloads_total_desc",
    "download": "downloads_total_desc",
    "popular": "popularity_total_desc",
    "popularity": "popularity_total_desc",
    "listens": "listens_total_desc",
    "listen": "listens_total_desc",
}
_RESERVED_SEARCH_PARAMS = {"client_id", "format", "audioformat", "audiodlformat", "include", "limit"}


@dataclass
class JamendoConfig:
    client_id: str
    client_secret: str = ""
    base_url: str = JAMENDO_BASE_URL
    download_dir: Path = field(default_factory=lambda: manifest.get_asset_root() / "downloads" / "jamendo")
    metadata_dir: Path = field(default_factory=lambda: manifest.get_asset_root() / "metadata" / "jamendo")
    timeout_seconds: float = 30.0
    user_agent: str = "oh-my-cassette jamendo-matcher/0.1"

    @classmethod
    def from_env(cls) -> "JamendoConfig":
        client_id = _runtime_env("JAMENDO_CLIENT_ID")
        if not client_id:
            raise CassetteError(
                "jamendo_client_id_missing",
                "JAMENDO_CLIENT_ID is required for Jamendo music matching",
                recoverable=True,
            )
        timeout_raw = _runtime_env("HTTP_TIMEOUT_SECONDS") or "30"
        try:
            timeout = float(timeout_raw)
        except ValueError:
            timeout = 30.0
        return cls(
            client_id=client_id,
            client_secret=_runtime_env("JAMENDO_CLIENT_SECRET"),
            base_url=(_runtime_env("JAMENDO_BASE_URL") or JAMENDO_BASE_URL).rstrip("/"),
            download_dir=_runtime_path("DOWNLOAD_DIR", manifest.get_asset_root() / "downloads" / "jamendo"),
            metadata_dir=_runtime_path("METADATA_DIR", manifest.get_asset_root() / "metadata" / "jamendo"),
            timeout_seconds=timeout,
            user_agent=_runtime_env("USER_AGENT") or "oh-my-cassette jamendo-matcher/0.1",
        )


@dataclass
class JamendoSearchStrategy:
    name: str
    search: str | None = None
    fuzzytags: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    vocalinstrumental: str | None = None
    acousticelectric: str | None = None
    speed: list[str] = field(default_factory=list)
    duration_min: int | None = None
    duration_max: int | None = None
    boost: str | None = None
    order: str | None = None
    limit: int | None = None
    type: str | None = None
    extra_params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int = 0) -> "JamendoSearchStrategy":
        if not isinstance(data, dict):
            raise CassetteError("jamendo_invalid_search_plan", "Each Jamendo strategy must be an object")
        vocal = _optional_enum(_field(data, "vocalinstrumental", "vocalInstrumental"), _VALID_VOCAL, "vocalInstrumental")
        acoustic = _optional_enum(_field(data, "acousticelectric", "acousticElectric"), _VALID_ACOUSTIC, "acousticElectric")
        speed = _string_list(_field(data, "speed") or [], "speed")
        invalid_speed = [item for item in speed if item not in _VALID_SPEED]
        if invalid_speed:
            raise CassetteError("jamendo_invalid_search_plan", f"Invalid Jamendo speed values: {', '.join(invalid_speed)}")
        limit = _optional_int(_field(data, "limit"), "limit")
        if limit is not None:
            limit = max(1, min(limit, 200))
        return cls(
            name=str(_field(data, "name") or f"strategy_{index + 1}").strip() or f"strategy_{index + 1}",
            search=_optional_str(_field(data, "search")),
            fuzzytags=_string_list(_field(data, "fuzzytags", "fuzzyTags") or [], "fuzzyTags"),
            tags=_string_list(_field(data, "tags") or [], "tags"),
            exclude_terms=_string_list(_field(data, "exclude_terms", "excludeTerms") or [], "excludeTerms"),
            vocalinstrumental=vocal,
            acousticelectric=acoustic,
            speed=speed,
            duration_min=_optional_int(_field(data, "duration_min", "durationMin"), "durationMin"),
            duration_max=_optional_int(_field(data, "duration_max", "durationMax"), "durationMax"),
            boost=_optional_boost(_field(data, "boost")),
            order=_optional_order(_field(data, "order")),
            limit=limit,
            type=_optional_type(_field(data, "type")),
            extra_params=_dict_or_empty(_field(data, "extra_params", "extraParams"), "extraParams"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JamendoSearchPlan:
    raw_user_query: str
    strategies: list[JamendoSearchStrategy]
    audio_format: str = _AUDIO_FORMAT
    download_format: str = _AUDIO_FORMAT
    require_downloadable: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JamendoSearchPlan":
        if not isinstance(data, dict):
            raise CassetteError("jamendo_invalid_search_plan", "Jamendo SearchPlan must be a JSON object")
        raw_query = str(_field(data, "raw_user_query", "rawUserQuery") or "").strip()
        if not raw_query:
            raise CassetteError("jamendo_invalid_search_plan", "Jamendo SearchPlan.rawUserQuery is required")
        raw_strategies = _field(data, "strategies", "search_strategies", "searchStrategies")
        if not isinstance(raw_strategies, list) or not raw_strategies:
            raise CassetteError("jamendo_invalid_search_plan", "Jamendo SearchPlan.strategies must contain at least one strategy")
        strategies = [JamendoSearchStrategy.from_dict(item, index) for index, item in enumerate(raw_strategies)]
        return cls(
            raw_user_query=raw_query,
            strategies=strategies,
            audio_format=str(_field(data, "audio_format", "audioFormat") or _AUDIO_FORMAT).strip() or _AUDIO_FORMAT,
            download_format=str(_field(data, "download_format", "downloadFormat") or _AUDIO_FORMAT).strip() or _AUDIO_FORMAT,
            require_downloadable=_bool_value(_field(data, "require_downloadable", "requireDownloadable", default=True)),
        )

    @classmethod
    def from_json_text(cls, text: str) -> "JamendoSearchPlan":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CassetteError("jamendo_invalid_search_plan_json", "Hermes returned invalid Jamendo SearchPlan JSON") from exc
        return cls.from_dict(parsed)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["strategies"] = [strategy.to_dict() for strategy in self.strategies]
        return data


@dataclass
class TrackCandidate:
    provider: str
    id: str
    name: str
    artist_id: str | None = None
    artist_name: str | None = None
    album_id: str | None = None
    album_name: str | None = None
    duration: int | None = None
    releasedate: str | None = None
    license_ccurl: str | None = None
    audio: str | None = None
    audiodownload: str | None = None
    audiodownload_allowed: bool | None = None
    shareurl: str | None = None
    shorturl: str | None = None
    image: str | None = None
    musicinfo: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    source_strategies: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_jamendo_result(cls, data: dict[str, Any], source_strategy: dict[str, Any]) -> "TrackCandidate":
        if not isinstance(data, dict):
            data = {}
        allowed = data.get("audiodownload_allowed")
        if isinstance(allowed, str):
            allowed = allowed.strip().lower() in {"1", "true", "yes"}
        return cls(
            provider="jamendo",
            id=str(data.get("id") or "").strip(),
            name=str(data.get("name") or "").strip(),
            artist_id=_optional_str(data.get("artist_id")),
            artist_name=_optional_str(data.get("artist_name")),
            album_id=_optional_str(data.get("album_id")),
            album_name=_optional_str(data.get("album_name")),
            duration=_optional_int(data.get("duration"), "duration"),
            releasedate=_optional_str(data.get("releasedate")),
            license_ccurl=_optional_str(data.get("license_ccurl")),
            audio=_optional_str(data.get("audio")),
            audiodownload=_optional_str(data.get("audiodownload")),
            audiodownload_allowed=allowed if isinstance(allowed, bool) else None,
            shareurl=_optional_str(data.get("shareurl")),
            shorturl=_optional_str(data.get("shorturl")),
            image=_optional_str(data.get("image")),
            musicinfo=_dict_or_empty(data.get("musicinfo"), "musicinfo"),
            stats=_dict_or_empty(data.get("stats"), "stats"),
            raw=data,
            source_strategies=[source_strategy],
        )

    def to_dict(self, *, include_raw: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not include_raw:
            data.pop("raw", None)
        return data

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "artistName": self.artist_name,
            "albumName": self.album_name,
            "duration": self.duration,
            "shareUrl": self.shareurl,
            "audiodownloadAllowed": self.audiodownload_allowed,
            "sourceStrategies": self.source_strategies,
        }


class HermesPlanRequired(Exception):
    def __init__(self, prompt: str) -> None:
        super().__init__("Hermes Jamendo SearchPlan JSON is required")
        self.prompt = prompt


class NoEligibleTracksError(CassetteError):
    def __init__(self) -> None:
        super().__init__("jamendo_no_eligible_tracks", "No eligible Jamendo tracks remained after filtering")


class HermesJamendoPlanner:
    def __init__(self, prompt_template: str | None = None) -> None:
        self.prompt_template = prompt_template or load_jamendo_plan_prompt_template()

    def prompt_for(self, user_query: str) -> str:
        return self.prompt_template.replace("{{USER_QUERY}}", user_query)

    def build_search_plan(
        self,
        user_query: str,
        hermes_json: str | dict[str, Any] | None = None,
        repair_json: str | dict[str, Any] | None = None,
    ) -> JamendoSearchPlan:
        if hermes_json is None:
            raise HermesPlanRequired(self.prompt_for(user_query))
        try:
            return _parse_plan_input(_with_raw_user_query(hermes_json, user_query))
        except CassetteError:
            if repair_json is None:
                raise
            return _parse_plan_input(_with_raw_user_query(repair_json, user_query))


def build_search_plan_from_form(
    *,
    user_query: str,
    search_terms: list[str],
    fuzzy_tags: list[str] | None = None,
    exclude_terms: list[str] | None = None,
    vocalinstrumental: str | None = None,
    limit: int | None = None,
) -> JamendoSearchPlan:
    terms = _dedupe_strings(search_terms)[:5]
    fuzzy = _dedupe_strings(fuzzy_tags or [])[:8]
    excludes = _dedupe_strings(exclude_terms or [])[:12]
    if not terms and fuzzy:
        terms = [" ".join(fuzzy[:4])]
    if not terms:
        raise CassetteError(
            "jamendo_search_form_required",
            "Jamendo fixed-form matching requires at least one English search term",
        )
    vocal = _optional_enum(vocalinstrumental, _VALID_VOCAL, "vocalInstrumental")
    safe_limit = max(1, min(int(limit or 10), 50))
    variants = [
        ("relevance_popularity", "popularity_total", "relevance"),
        ("relevance_downloads", "downloads_total", "relevance"),
        ("popular_order", None, "popularity_total_desc"),
    ]
    strategies: list[JamendoSearchStrategy] = []
    for term_index, term in enumerate(terms):
        active_variants = variants if term_index < 3 else variants[:1]
        for variant_name, boost, order in active_variants:
            strategies.append(JamendoSearchStrategy(
                name=f"fixed_form_{term_index + 1}_{variant_name}",
                search=term,
                fuzzytags=fuzzy,
                tags=[],
                exclude_terms=excludes,
                vocalinstrumental=vocal,
                boost=boost,
                order=order,
                limit=safe_limit,
                type="single albumtrack",
            ))
    return JamendoSearchPlan(
        raw_user_query=user_query,
        strategies=strategies,
        audio_format=_AUDIO_FORMAT,
        download_format=_AUDIO_FORMAT,
        require_downloadable=True,
    )


class JamendoClient:
    def __init__(self, client_id: str, timeout: float = 30.0, user_agent: str = "oh-my-cassette jamendo-matcher/0.1", base_url: str = JAMENDO_BASE_URL) -> None:
        self.client_id = client_id
        self.timeout = timeout
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")

    def search_tracks(self, plan: JamendoSearchPlan, *, limit_override: int | None = None) -> list[TrackCandidate]:
        candidates: list[TrackCandidate] = []
        strategy_errors: list[dict[str, Any]] = []
        for strategy in plan.strategies:
            params = self._track_params(plan, strategy, limit_override=limit_override)
            payload = self._get_json("/tracks/", params)
            headers = payload.get("headers") if isinstance(payload, dict) else {}
            status = str((headers or {}).get("status") or "").lower()
            if status and status != "success":
                strategy_errors.append({
                    "strategy": strategy.name,
                    **_jamendo_error_details(headers),
                })
                continue
            results = payload.get("results") if isinstance(payload, dict) else []
            if not isinstance(results, list):
                raise CassetteError("jamendo_api_error", "Jamendo /tracks returned an invalid results payload")
            source_strategy = strategy.to_dict()
            for item in results:
                candidates.append(TrackCandidate.from_jamendo_result(item, source_strategy))
        if candidates:
            return candidates
        if strategy_errors and len(strategy_errors) >= len(plan.strategies):
            raise CassetteError(
                "jamendo_api_error",
                "All Jamendo /tracks strategies returned a non-success status",
                {"strategy_errors": strategy_errors[:5]},
            )
        return candidates

    def download_track_file(self, track: TrackCandidate, output_dir: Path, audioformat: str = _AUDIO_FORMAT) -> Path:
        if track.audiodownload_allowed is False:
            raise CassetteError("jamendo_download_not_allowed", "Jamendo track does not allow audio download")
        output_dir.mkdir(parents=True, exist_ok=True)
        dest = output_dir / safe_jamendo_filename(track)
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        url = track.audiodownload
        if not url and track.audiodownload_allowed is True:
            params = {
                "client_id": self.client_id,
                "id": track.id,
                "audioformat": audioformat,
                "action": "download",
            }
            url = f"{self.base_url}/tracks/file/?{urlencode(params)}"
        if not url:
            raise CassetteError("jamendo_download_url_missing", "Jamendo track did not include an audiodownload URL")
        part_path = dest.with_name(dest.name + ".part")
        try:
            self._download_url(url, part_path)
            if not part_path.exists() or part_path.stat().st_size <= 0:
                raise CassetteError("jamendo_download_empty", "Jamendo MP3 download was empty")
            os.replace(part_path, dest)
            return dest
        except Exception:
            try:
                part_path.unlink()
            except OSError:
                pass
            raise

    def _track_params(self, plan: JamendoSearchPlan, strategy: JamendoSearchStrategy, *, limit_override: int | None = None) -> dict[str, Any]:
        limit = limit_override if limit_override is not None else strategy.limit if strategy.limit is not None else 10
        limit = max(1, min(int(limit), 200))
        params: dict[str, Any] = {
            "client_id": self.client_id,
            "format": "json",
            "audioformat": plan.audio_format or _AUDIO_FORMAT,
            "audiodlformat": plan.download_format or _AUDIO_FORMAT,
            "include": "licenses+musicinfo+stats",
            "limit": limit,
        }
        _add_optional(params, "search", strategy.search)
        _add_optional(params, "fuzzytags", " ".join(strategy.fuzzytags) if strategy.fuzzytags else None)
        _add_optional(params, "tags", " ".join(strategy.tags) if strategy.tags else None)
        _add_optional(params, "vocalinstrumental", strategy.vocalinstrumental)
        _add_optional(params, "acousticelectric", strategy.acousticelectric)
        _add_optional(params, "speed", " ".join(strategy.speed) if strategy.speed else None)
        if strategy.duration_min is not None and strategy.duration_max is not None:
            params["durationbetween"] = f"{strategy.duration_min}_{strategy.duration_max}"
        _add_optional(params, "boost", strategy.boost)
        _add_optional(params, "order", strategy.order)
        _add_optional(params, "type", strategy.type)
        for key, value in strategy.extra_params.items():
            key_text = str(key).strip()
            if key_text and key_text not in _RESERVED_SEARCH_PARAMS and value is not None:
                params[key_text] = value
        return params

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}?{urlencode(params, doseq=True)}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": self.user_agent})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read()
        except HTTPError as exc:
            raise CassetteError("jamendo_http_error", "Jamendo HTTP request failed", {"endpoint": path, "status": exc.code}) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CassetteError("jamendo_network_error", "Jamendo HTTP request failed", {"endpoint": path, "type": type(exc).__name__}) from exc
        if int(status) < 200 or int(status) >= 300:
            raise CassetteError("jamendo_http_error", "Jamendo HTTP request failed", {"endpoint": path, "status": int(status)})
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise CassetteError("jamendo_invalid_json", "Jamendo returned invalid JSON", {"endpoint": path}) from exc
        if not isinstance(parsed, dict):
            raise CassetteError("jamendo_invalid_json", "Jamendo returned a non-object JSON payload", {"endpoint": path})
        return parsed

    def _download_url(self, url: str, part_path: Path) -> None:
        request = Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urlopen(request, timeout=self.timeout) as response, part_path.open("wb") as fh:
                status = getattr(response, "status", 200)
                if int(status) < 200 or int(status) >= 300:
                    raise CassetteError("jamendo_download_http_error", "Jamendo MP3 download failed", {"status": int(status)})
                while True:
                    chunk = response.read(1024 * 128)
                    if not chunk:
                        break
                    fh.write(chunk)
        except HTTPError as exc:
            raise CassetteError("jamendo_download_http_error", "Jamendo MP3 download failed", {"status": exc.code}) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CassetteError("jamendo_download_failed", "Jamendo MP3 download failed", {"type": type(exc).__name__}) from exc


def filter_eligible_tracks(candidates: list[TrackCandidate], plan: JamendoSearchPlan) -> list[TrackCandidate]:
    deduped: dict[str, TrackCandidate] = {}
    for candidate in candidates:
        if not candidate.id or not candidate.name:
            continue
        if candidate.audiodownload_allowed is False:
            continue
        if plan.require_downloadable and not (candidate.audiodownload_allowed is True or candidate.audiodownload):
            continue
        if _matches_excluded_terms(candidate, plan):
            continue
        if not _matches_duration_constraints(candidate, plan):
            continue
        existing = deduped.get(candidate.id)
        if existing is None:
            deduped[candidate.id] = candidate
        else:
            _merge_source_strategies(existing, candidate)
    return list(deduped.values())


def sample_random_track(candidates: list[TrackCandidate], seed: int | None = None) -> tuple[TrackCandidate, int]:
    if not candidates:
        raise NoEligibleTracksError()
    effective_seed = seed if seed is not None else random.SystemRandom().randrange(0, 2**63)
    pool = sorted(candidates, key=lambda item: item.id)
    selected = pool[random.Random(effective_seed).randrange(len(pool))]
    return selected, effective_seed


def save_download_metadata(
    *,
    selected: TrackCandidate,
    selected_at: str,
    seed: int,
    plan: JamendoSearchPlan,
    candidate_pool: list[TrackCandidate],
    local_file: Path,
    metadata_dir: Path,
    asset_root: Path | None = None,
    manifest_asset: dict[str, Any] | None = None,
) -> Path:
    metadata_dir.mkdir(parents=True, exist_ok=True)
    timestamp = selected_at.replace(":", "").replace("-", "")
    path = metadata_dir / f"{selected.id}_{timestamp}.json"
    local_file_value = _display_path(local_file, asset_root or manifest.get_asset_root())
    payload = {
        "selectedAt": selected_at,
        "seed": seed,
        "rawUserQuery": plan.raw_user_query,
        "searchPlan": plan.to_dict(),
        "candidateCount": len(candidate_pool),
        "selectedTrack": selected.to_dict(include_raw=False),
        "selectedTrackRaw": selected.raw,
        "sourceStrategies": selected.source_strategies,
        "localFile": local_file_value,
        "jamendoShareUrl": selected.shareurl,
        "licenseCcUrl": selected.license_ccurl,
        "audiodownloadAllowed": selected.audiodownload_allowed,
        "candidatePoolSnapshot": [item.snapshot() for item in sorted(candidate_pool, key=lambda item: item.id)],
        "manifestAsset": _scrub_manifest_asset(manifest_asset) if manifest_asset else None,
    }
    fd, tmp_name = tempfile.mkstemp(prefix=".jamendo-metadata.", suffix=".json", dir=str(metadata_dir))
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


def match_jamendo_music(
    *,
    user_query: str,
    search_plan: JamendoSearchPlan,
    download: bool = True,
    seed: int | None = None,
    limit_override: int | None = None,
    output_dir: Path | None = None,
    session_id: str | None = None,
    config: JamendoConfig | None = None,
) -> dict[str, Any]:
    cfg = config or JamendoConfig.from_env()
    client = JamendoClient(cfg.client_id, cfg.timeout_seconds, cfg.user_agent, cfg.base_url)
    candidates, effective_plan, fallbacks = _search_candidates_with_fallbacks(
        client,
        search_plan,
        limit_override=limit_override,
    )
    eligible = filter_eligible_tracks(candidates, effective_plan)
    if not eligible and candidates:
        relaxed_filter_plan = _relaxed_filter_plan(effective_plan)
        relaxed_eligible = filter_eligible_tracks(candidates, relaxed_filter_plan)
        if relaxed_eligible:
            eligible = relaxed_eligible
            effective_plan = relaxed_filter_plan
            fallbacks.append({
                "code": "jamendo_filter_relaxed",
                "message": "Relaxed local duration/exclude filters after Jamendo returned candidates but none remained eligible.",
            })
    if not download:
        return {
            "searchPlan": effective_plan.to_dict(),
            "originalSearchPlan": search_plan.to_dict() if effective_plan.to_dict() != search_plan.to_dict() else None,
            "eligibleCandidates": [item.to_dict(include_raw=False) for item in eligible],
            "candidateCount": len(eligible),
            "fallbacks": fallbacks,
        }
    selected, effective_seed = sample_random_track(eligible, seed)
    selected_at = _now_iso()
    download_dir = _safe_output_dir(output_dir or cfg.download_dir)
    local_file = client.download_track_file(selected, download_dir, search_plan.download_format or _AUDIO_FORMAT)
    manifest_asset: dict[str, Any] | None = None
    if session_id:
        manifest_asset = manifest.ingest_internal_asset(
            str(local_file),
            session_id=session_id,
            original_name=safe_jamendo_filename(selected),
            media_type="audio",
            caption=f"Jamendo matched BGM: {selected.artist_name or 'unknown artist'} - {selected.name}.",
            metadata={
                "source": "jamendo",
                "track_id": selected.id,
                "artist": selected.artist_name or "",
                "title": selected.name,
                "shareurl": selected.shareurl or "",
                "license_ccurl": selected.license_ccurl or "",
                "seed": effective_seed,
            },
        )
    metadata_path = save_download_metadata(
        selected=selected,
        selected_at=selected_at,
        seed=effective_seed,
        plan=effective_plan,
        candidate_pool=eligible,
        local_file=local_file,
        metadata_dir=_safe_output_dir(cfg.metadata_dir),
        manifest_asset=manifest_asset,
    )
    data: dict[str, Any] = {
        "track_id": selected.id,
        "file_path": _display_path(local_file, manifest.get_asset_root()),
        "metadata_path": _display_path(metadata_path, manifest.get_asset_root()),
        "selected_at": selected_at,
        "seed": effective_seed,
        "search_plan": effective_plan.to_dict(),
        "candidate_count": len(eligible),
        "selected_track": selected.to_dict(include_raw=False),
        "candidate_pool_snapshot": [item.snapshot() for item in sorted(eligible, key=lambda item: item.id)],
        "manifest_asset": _scrub_manifest_asset(manifest_asset) if manifest_asset else None,
    }
    data.update({
        "status": "downloaded",
        "provider": "jamendo",
        "effective_instruction": _instruction_with_jamendo_bgm(search_plan.raw_user_query, selected),
        "user_message": _jamendo_status_message(selected),
        "fallbacks": fallbacks,
        "hermes_next_step": (
            "Use effective_instruction directly as the edit instruction for cassette_make_prompt, "
            "or as the source text for prompt optimization if optimization is still enabled. "
            "If this tool returned an error, fall back to the existing Free To Use cassette_match_bgm flow."
        ),
    })
    return data


def safe_jamendo_filename(track: TrackCandidate) -> str:
    artist = track.artist_name or "Jamendo"
    title = track.name or "track"
    track_id = track.id or "unknown"
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", f"{artist} - {title} - {track_id}")
    stem = re.sub(r"\s+", " ", stem).strip(" ._")
    if not stem:
        stem = f"jamendo-{track_id}"
    return f"{stem[:160]}.mp3"


def _search_candidates_with_fallbacks(
    client: JamendoClient,
    plan: JamendoSearchPlan,
    *,
    limit_override: int | None = None,
) -> tuple[list[TrackCandidate], JamendoSearchPlan, list[dict[str, str]]]:
    fallbacks: list[dict[str, str]] = []
    relaxed_plan = _relaxed_api_plan(plan)
    broad_plan = _broad_api_plan(relaxed_plan)
    attempts = [
        ("jamendo_search_attempt_1", "Initial fixed-form Jamendo search.", plan),
        ("jamendo_search_attempt_2_relaxed", "Retried Jamendo search with strict API parameters relaxed.", relaxed_plan),
        ("jamendo_search_attempt_3_broad", "Retried Jamendo search with broader relevance/popularity/download ordering.", broad_plan),
    ]
    last_plan = plan
    for index, (code, message, attempt_plan) in enumerate(attempts):
        if index > 0 and attempt_plan.to_dict() == last_plan.to_dict():
            continue
        last_plan = attempt_plan
        try:
            candidates = client.search_tracks(attempt_plan, limit_override=limit_override)
        except CassetteError as exc:
            if exc.code != "jamendo_api_error":
                raise
            fallbacks.append({
                "code": code,
                "message": f"{message} Jamendo API rejected this attempt: {exc.code}.",
            })
            continue
        if candidates:
            if index > 0:
                fallbacks.append({"code": code, "message": message})
            return candidates, attempt_plan, fallbacks
        fallbacks.append({
            "code": code,
            "message": f"{message} Jamendo returned zero candidates.",
        })

    return [], last_plan, fallbacks


def _relaxed_api_plan(plan: JamendoSearchPlan) -> JamendoSearchPlan:
    strategies: list[JamendoSearchStrategy] = []
    for strategy in plan.strategies:
        strategies.append(JamendoSearchStrategy(
            name=f"{strategy.name}_relaxed",
            search=strategy.search,
            fuzzytags=_dedupe_strings([*strategy.fuzzytags, *strategy.tags]),
            tags=[],
            exclude_terms=list(strategy.exclude_terms),
            vocalinstrumental=strategy.vocalinstrumental,
            acousticelectric=None,
            speed=[],
            duration_min=strategy.duration_min,
            duration_max=strategy.duration_max,
            boost=strategy.boost,
            order=strategy.order,
            limit=strategy.limit,
            type=None,
            extra_params={},
        ))
    return JamendoSearchPlan(
        raw_user_query=plan.raw_user_query,
        strategies=strategies,
        audio_format=plan.audio_format,
        download_format=plan.download_format,
        require_downloadable=plan.require_downloadable,
    )


def _broad_api_plan(plan: JamendoSearchPlan) -> JamendoSearchPlan:
    strategies: list[JamendoSearchStrategy] = []
    variants = [
        ("relevance", "popularity_total", "relevance"),
        ("popular", None, "popularity_total_desc"),
        ("downloads", None, "downloads_total_desc"),
    ]
    for strategy in plan.strategies:
        for suffix, boost, order in variants:
            strategies.append(JamendoSearchStrategy(
                name=f"{strategy.name}_broad_{suffix}",
                search=strategy.search,
                fuzzytags=list(strategy.fuzzytags),
                tags=[],
                exclude_terms=list(strategy.exclude_terms),
                vocalinstrumental=None,
                acousticelectric=None,
                speed=[],
                duration_min=None,
                duration_max=None,
                boost=boost,
                order=order,
                limit=strategy.limit,
                type=None,
                extra_params={},
            ))
    return JamendoSearchPlan(
        raw_user_query=plan.raw_user_query,
        strategies=strategies,
        audio_format=plan.audio_format,
        download_format=plan.download_format,
        require_downloadable=plan.require_downloadable,
    )


def _relaxed_filter_plan(plan: JamendoSearchPlan) -> JamendoSearchPlan:
    strategies: list[JamendoSearchStrategy] = []
    for strategy in plan.strategies:
        strategies.append(JamendoSearchStrategy(
            name=f"{strategy.name}_filter_relaxed",
            search=strategy.search,
            fuzzytags=list(strategy.fuzzytags),
            tags=list(strategy.tags),
            exclude_terms=[],
            vocalinstrumental=strategy.vocalinstrumental,
            acousticelectric=strategy.acousticelectric,
            speed=list(strategy.speed),
            duration_min=None,
            duration_max=None,
            boost=strategy.boost,
            order=strategy.order,
            limit=strategy.limit,
            type=strategy.type,
            extra_params=dict(strategy.extra_params),
        ))
    return JamendoSearchPlan(
        raw_user_query=plan.raw_user_query,
        strategies=strategies,
        audio_format=plan.audio_format,
        download_format=plan.download_format,
        require_downloadable=plan.require_downloadable,
    )


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        key = text.lower()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _instruction_with_jamendo_bgm(instruction: str, track: TrackCandidate) -> str:
    artist = (track.artist_name or "Jamendo").strip() or "Jamendo"
    title = (track.name or "matched BGM").strip() or "matched BGM"
    return (
        f"{instruction}\n\n"
        f"请添加已上传的 Jamendo 智能匹配 BGM「{artist} - {title}」作为背景音乐，"
        "根据视频节奏自动调整起止、音量、淡入淡出和与原声的平衡；"
        "如果用户原指令中有更明确的音乐要求，以用户明确要求优先。"
    )


def _jamendo_status_message(track: TrackCandidate) -> str:
    artist = track.artist_name or "未知艺术家"
    title = track.name or "未知曲目"
    return f"已通过 Jamendo 智能匹配 BGM：{artist} - {title}。"


def load_jamendo_plan_prompt_template() -> str:
    try:
        return JAMENDO_PLAN_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return _default_jamendo_plan_prompt()


def _parse_plan_input(value: str | dict[str, Any]) -> JamendoSearchPlan:
    if isinstance(value, dict):
        return JamendoSearchPlan.from_dict(value)
    return JamendoSearchPlan.from_json_text(str(value or "").strip())


def _with_raw_user_query(value: str | dict[str, Any], user_query: str) -> str | dict[str, Any]:
    def patch(data: dict[str, Any]) -> dict[str, Any]:
        copied = dict(data)
        if not str(_field(copied, "raw_user_query", "rawUserQuery") or "").strip():
            copied["rawUserQuery"] = user_query
        return copied

    if isinstance(value, dict):
        return patch(value)
    text = str(value or "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, dict):
        return patch(parsed)
    return value


def _runtime_env(name: str) -> str:
    return str(os.getenv(name, "") or notifier._runtime_env(name)).strip()


def _runtime_path(name: str, default: Path) -> Path:
    raw = _runtime_env(name)
    if not raw:
        return default
    path = Path(os.path.expandvars(raw)).expanduser()
    if not path.is_absolute():
        return manifest.get_asset_root() / path
    return path


def _safe_output_dir(path: Path) -> Path:
    root = manifest.get_asset_root()
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = root / expanded
    resolved = expanded.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CassetteError("jamendo_output_dir_outside_asset_root", "Jamendo output directories must live under CASSETTE_ASSET_ROOT") from exc
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name


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


def _matches_excluded_terms(candidate: TrackCandidate, plan: JamendoSearchPlan) -> bool:
    terms: set[str] = set()
    for strategy in _candidate_strategy_dicts(candidate, plan):
        for term in strategy.get("exclude_terms") or strategy.get("excludeTerms") or []:
            if str(term).strip():
                terms.add(str(term).lower().strip())
    if not terms:
        return False
    haystack = " ".join(filter(None, [candidate.name, candidate.artist_name, candidate.album_name])).lower()
    return any(term in haystack for term in terms)


def _matches_duration_constraints(candidate: TrackCandidate, plan: JamendoSearchPlan) -> bool:
    if candidate.duration is None:
        return True
    constrained = False
    for strategy in _candidate_strategy_dicts(candidate, plan):
        minimum = strategy.get("duration_min")
        maximum = strategy.get("duration_max")
        if minimum is None and maximum is None:
            continue
        constrained = True
        if minimum is not None and candidate.duration < int(minimum):
            continue
        if maximum is not None and candidate.duration > int(maximum):
            continue
        return True
    return not constrained


def _candidate_strategy_dicts(candidate: TrackCandidate, plan: JamendoSearchPlan) -> list[dict[str, Any]]:
    if candidate.source_strategies:
        candidate_has_constraints = any(
            (strategy.get("exclude_terms") or strategy.get("excludeTerms") or strategy.get("duration_min") is not None or strategy.get("duration_max") is not None)
            for strategy in candidate.source_strategies
        )
        if candidate_has_constraints:
            return candidate.source_strategies
    return [strategy.to_dict() for strategy in plan.strategies]


def _merge_source_strategies(existing: TrackCandidate, new: TrackCandidate) -> None:
    seen = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in existing.source_strategies}
    for strategy in new.source_strategies:
        key = json.dumps(strategy, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            existing.source_strategies.append(strategy)
            seen.add(key)


def _field(data: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return default


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CassetteError("jamendo_invalid_search_plan", f"Jamendo {field_name} must be an integer") from exc


def _optional_enum(value: Any, valid: set[str], field_name: str) -> str | None:
    text = _optional_str(value)
    if text is None:
        return None
    text = text.lower()
    if text in _NO_CONSTRAINT_VALUES:
        return None
    if text not in valid:
        raise CassetteError("jamendo_invalid_search_plan", f"Invalid Jamendo {field_name}: {text}")
    return text


def _optional_boost(value: Any) -> str | None:
    for text in _candidate_strings(value):
        normalized = _BOOST_ALIASES.get(text, text)
        if normalized in _VALID_BOOSTS:
            return normalized
    return None


def _optional_order(value: Any) -> str | None:
    for text in _candidate_strings(value):
        normalized = _ORDER_ALIASES.get(text, text)
        if normalized in {"relevance", "rating_desc", "rating_asc", "name_desc", "name_asc", "releasedate_desc", "releasedate_asc", "duration_desc", "duration_asc"}:
            return normalized
        if normalized in _VALID_BOOSTS:
            return normalized
        if normalized.endswith("_asc") or normalized.endswith("_desc"):
            base = normalized.rsplit("_", 1)[0]
            if base in _VALID_BOOSTS:
                return normalized
    return None


def _optional_type(value: Any) -> str | None:
    types: list[str] = []
    for text in _candidate_strings(value):
        for item in re.split(r"[\s,]+", text):
            if item in _VALID_TYPES and item not in types:
                types.append(item)
    if not types:
        return None
    return " ".join(types)


def _candidate_strings(value: Any) -> list[str]:
    if value is None:
        return []
    raw_values = value if isinstance(value, list) else [value]
    items: list[str] = []
    for item in raw_values:
        text = str(item).strip().lower()
        if text in _NO_CONSTRAINT_VALUES:
            continue
        items.append(text)
    return items


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise CassetteError("jamendo_invalid_search_plan", f"Jamendo {field_name} must be a list of strings")
    cleaned: list[str] = []
    for item in values:
        text = str(item).strip().lower()
        if text in _NO_CONSTRAINT_VALUES:
            continue
        cleaned.append(str(item).strip())
    return cleaned


def _dict_or_empty(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CassetteError("jamendo_invalid_search_plan", f"Jamendo {field_name} must be an object")
    return dict(value)


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _jamendo_error_details(headers: Any) -> dict[str, Any]:
    details: dict[str, Any] = {}
    if not isinstance(headers, dict):
        return details
    for key in ("status", "code", "error", "error_message", "message", "warnings"):
        value = headers.get(key)
        if value is not None:
            details[key] = _safe_detail_value(value)
    return details


def _safe_detail_value(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_detail_value(item) for item in value[:5]]
    if isinstance(value, dict):
        return {str(key)[:64]: _safe_detail_value(item) for key, item in list(value.items())[:10]}
    text = str(value)
    text = re.sub(r"(?i)(client_id|client_secret|secret|token|key)=([^&\s]+)", r"\1=<redacted>", text)
    text = re.sub(r"(?i)(client_id|client_secret|secret|token|key)['\"]?\s*:\s*['\"]?[^,'\"\s}]+", r"\1:<redacted>", text)
    if len(text) > 240:
        return f"{text[:120]}...<redacted:{len(text)} chars>...{text[-40:]}"
    return text


def _add_optional(params: dict[str, Any], key: str, value: Any) -> None:
    if value is not None and str(value).strip():
        params[key] = value


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_jamendo_plan_prompt() -> str:
    return """你是 Jamendo 音乐搜索策略生成器。
你的任务是把用户的自然语言音乐需求转换成 Jamendo API /tracks 可用的搜索策略。
只返回严格 JSON，不要返回 Markdown，不要解释。

用户需求：
{{USER_QUERY}}

请返回如下 JSON：

{
  "rawUserQuery": "...",
  "audioFormat": "mp32",
  "downloadFormat": "mp32",
  "requireDownloadable": true,
  "strategies": [
    {
      "name": "relevance_fuzzy",
      "search": null,
      "fuzzyTags": ["ambient", "electronic"],
      "tags": [],
      "excludeTerms": [],
      "vocalInstrumental": "instrumental",
      "acousticElectric": null,
      "speed": [],
      "durationMin": null,
      "durationMax": null,
      "boost": "popularity_total",
      "order": "relevance",
      "limit": 10,
      "type": "single albumtrack",
      "extraParams": {}
    }
  ]
}

规则：
- tags、fuzzyTags、search 需要使用适合 Jamendo 的英文词。
- 可以生成多个 strategies，用于从不同角度搜索。
- 优先生成 2 到 5 个 strategies。
- 每个 strategy 的 limit 建议为 10，除非用户明确要求更多。
- 不要添加用户没有表达的硬性限制。
- 不要凭空添加音乐长度限制。
- 如果用户没有提到时长，durationMin 和 durationMax 必须为 null。
- 如果用户明确要求短音乐、长音乐或具体秒数，才设置 durationMin/durationMax。
- 如果用户明确要求纯音乐/无人声，vocalInstrumental 可以设为 "instrumental"。
- 如果用户明确要求有人声，vocalInstrumental 可以设为 "vocal"。
- 如果不确定是否有人声，vocalInstrumental 设为 null。
- speed 只能使用 verylow, low, medium, high, veryhigh。
- vocalInstrumental 只能使用 vocal, instrumental 或 null。
- acousticElectric 只能使用 acoustic, electric 或 null。
- order 可以使用 relevance、popularity_total_desc、downloads_total_desc、downloads_month_desc、listens_total_desc、listens_month_desc 等 Jamendo 支持的排序。
- boost 可以使用 popularity_total、downloads_total、downloads_month、listens_total、popularity_month 等 Jamendo 支持的 boost。
- 不要生成解释文本。
- 不要把 JSON 放进代码块。"""
