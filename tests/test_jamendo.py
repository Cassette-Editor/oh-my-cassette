from __future__ import annotations

import json
from pathlib import Path

import pytest

from cassette import jamendo, tools
from cassette.errors import CassetteError


def _plan_dict(**strategy_overrides):
    strategy = {
        "name": "relevance_fuzzy",
        "search": None,
        "fuzzyTags": ["ambient", "electronic"],
        "tags": [],
        "excludeTerms": [],
        "vocalInstrumental": None,
        "acousticElectric": None,
        "speed": [],
        "durationMin": None,
        "durationMax": None,
        "boost": "popularity_total",
        "order": "relevance",
        "limit": 10,
        "type": "single albumtrack",
        "extraParams": {},
    }
    strategy.update(strategy_overrides)
    return {
        "rawUserQuery": "安静、未来感、适合游戏菜单的背景音乐",
        "audioFormat": "mp32",
        "downloadFormat": "mp32",
        "requireDownloadable": True,
        "strategies": [strategy],
    }


def _track(track_id: str, **overrides) -> jamendo.TrackCandidate:
    data = {
        "id": track_id,
        "name": f"Track {track_id}",
        "artist_id": f"artist-{track_id}",
        "artist_name": f"Artist {track_id}",
        "album_id": f"album-{track_id}",
        "album_name": f"Album {track_id}",
        "duration": 120,
        "audiodownload": f"https://download.test/{track_id}.mp3",
        "audiodownload_allowed": True,
        "shareurl": f"https://jamendo.test/track/{track_id}",
    }
    data.update(overrides)
    source = jamendo.JamendoSearchStrategy.from_dict(_plan_dict()["strategies"][0]).to_dict()
    return jamendo.TrackCandidate.from_jamendo_result(data, source)


def test_hermes_planner_parses_mock_json_without_rule_mapping():
    planner = jamendo.HermesJamendoPlanner(prompt_template="USER={{USER_QUERY}}")
    plan = planner.build_search_plan(
        "安静、未来感、适合游戏菜单的背景音乐",
        json.dumps(_plan_dict(), ensure_ascii=False),
    )

    assert plan.raw_user_query == "安静、未来感、适合游戏菜单的背景音乐"
    assert plan.strategies[0].fuzzytags == ["ambient", "electronic"]
    assert plan.strategies[0].duration_min is None


def test_hermes_planner_requires_hermes_json_and_does_not_fallback():
    planner = jamendo.HermesJamendoPlanner(prompt_template="USER={{USER_QUERY}}")

    with pytest.raises(jamendo.HermesPlanRequired) as required:
        planner.build_search_plan("中文需求")
    assert "中文需求" in required.value.prompt

    with pytest.raises(CassetteError) as invalid:
        planner.build_search_plan("中文需求", "{not json")
    assert invalid.value.code == "jamendo_invalid_search_plan_json"


def test_hermes_planner_uses_repair_json_once():
    planner = jamendo.HermesJamendoPlanner(prompt_template="USER={{USER_QUERY}}")

    plan = planner.build_search_plan("中文需求", "{not json", json.dumps(_plan_dict()))

    assert plan.strategies[0].name == "relevance_fuzzy"


def test_hermes_plan_normalizes_common_llm_jamendo_parameter_mistakes():
    planner = jamendo.HermesJamendoPlanner(prompt_template="USER={{USER_QUERY}}")

    plan = planner.build_search_plan(
        "温柔男声流行歌",
        {
            "audioFormat": "mp32",
            "downloadFormat": "mp32",
            "requireDownloadable": True,
            "strategies": [
                {
                    "name": "gentle_pop",
                    "search": "gentle male vocal pop",
                    "fuzzyTags": ["male vocal", "pop"],
                    "acousticElectric": "any",
                    "speed": "any",
                    "boost": ["downloads", "popularity_total"],
                    "order": "popularity_month",
                    "type": "search",
                    "limit": 30,
                }
            ],
        },
    )
    strategy = plan.strategies[0]
    params = jamendo.JamendoClient("client-id")._track_params(plan, strategy)

    assert plan.raw_user_query == "温柔男声流行歌"
    assert strategy.acousticelectric is None
    assert strategy.speed == []
    assert strategy.boost == "downloads_total"
    assert strategy.order == "popularity_month"
    assert strategy.type is None
    assert params["boost"] == "downloads_total"
    assert params["order"] == "popularity_month"
    assert "acousticelectric" not in params
    assert "speed" not in params
    assert "type" not in params


def test_jamendo_client_builds_strategy_params_without_unset_constraints():
    calls = []

    class FakeClient(jamendo.JamendoClient):
        def _get_json(self, path, params):
            calls.append((path, dict(params)))
            return {
                "headers": {"status": "success"},
                "results": [{"id": "1", "name": "One", "audiodownload_allowed": True}],
            }

    plan = jamendo.JamendoSearchPlan.from_dict(_plan_dict())
    results = FakeClient("client-id").search_tracks(plan)

    assert len(results) == 1
    params = calls[0][1]
    assert params["audioformat"] == "mp32"
    assert params["audiodlformat"] == "mp32"
    assert params["include"] == "licenses+musicinfo+stats"
    assert params["fuzzytags"] == "ambient electronic"
    assert "durationbetween" not in params
    assert "vocalinstrumental" not in params
    assert "speed" not in params


def test_jamendo_client_adds_duration_vocal_and_speed_only_when_hermes_provides_them():
    calls = []

    class FakeClient(jamendo.JamendoClient):
        def _get_json(self, path, params):
            calls.append(dict(params))
            return {"headers": {"status": "success"}, "results": []}

    plan = jamendo.JamendoSearchPlan.from_dict(
        _plan_dict(
            durationMin=30,
            durationMax=180,
            vocalInstrumental="instrumental",
            speed=["low", "medium"],
        )
    )
    FakeClient("client-id").search_tracks(plan)

    params = calls[0]
    assert params["durationbetween"] == "30_180"
    assert params["vocalinstrumental"] == "instrumental"
    assert params["speed"] == "low medium"


def test_jamendo_client_continues_after_one_strategy_api_failure():
    calls = []

    class FakeClient(jamendo.JamendoClient):
        def _get_json(self, path, params):
            calls.append(dict(params))
            if params.get("search") == "bad search":
                return {
                    "headers": {
                        "status": "failed",
                        "code": 3,
                        "error_message": "bad strategy",
                    },
                    "results": [],
                }
            return {
                "headers": {"status": "success"},
                "results": [{"id": "2", "name": "Two", "audiodownload_allowed": True}],
            }

    plan_data = _plan_dict(search="bad search")
    plan_data["strategies"].append({**_plan_dict(search="good search")["strategies"][0], "name": "fallback"})
    results = FakeClient("client-id").search_tracks(jamendo.JamendoSearchPlan.from_dict(plan_data))

    assert [item.id for item in results] == ["2"]
    assert [call["search"] for call in calls] == ["bad search", "good search"]


def test_jamendo_search_fallback_relaxes_api_params_when_primary_empty():
    calls = []

    class FakeClient(jamendo.JamendoClient):
        def search_tracks(self, plan, *, limit_override=None):
            del limit_override
            calls.append(plan.to_dict())
            if len(calls) == 1:
                return []
            return [_track("relaxed")]

    plan = jamendo.JamendoSearchPlan.from_dict(
        _plan_dict(
            tags=["ambient"],
            acousticElectric="acoustic",
            speed=["low"],
            type="single albumtrack",
        )
    )

    candidates, effective_plan, fallbacks = jamendo._search_candidates_with_fallbacks(FakeClient("client-id"), plan)

    assert [item.id for item in candidates] == ["relaxed"]
    assert effective_plan.strategies[0].tags == []
    assert effective_plan.strategies[0].fuzzytags == ["ambient", "electronic"]
    assert effective_plan.strategies[0].acousticelectric is None
    assert effective_plan.strategies[0].speed == []
    assert effective_plan.strategies[0].type is None
    assert [item["code"] for item in fallbacks[:2]] == [
        "jamendo_search_attempt_1",
        "jamendo_search_attempt_2_relaxed",
    ]


def test_jamendo_search_uses_three_attempt_budget_before_empty_result():
    calls = []

    class FakeClient(jamendo.JamendoClient):
        def search_tracks(self, plan, *, limit_override=None):
            del limit_override
            calls.append(plan.to_dict())
            return []

    plan = jamendo.JamendoSearchPlan.from_dict(_plan_dict())
    candidates, effective_plan, fallbacks = jamendo._search_candidates_with_fallbacks(FakeClient("client-id"), plan)

    assert candidates == []
    assert effective_plan.strategies[0].name.endswith("_broad_relevance")
    assert len(calls) == 3
    assert [item["code"] for item in fallbacks] == [
        "jamendo_search_attempt_1",
        "jamendo_search_attempt_2_relaxed",
        "jamendo_search_attempt_3_broad",
    ]


def test_jamendo_search_can_succeed_on_third_broadened_attempt():
    calls = []

    class FakeClient(jamendo.JamendoClient):
        def search_tracks(self, plan, *, limit_override=None):
            del limit_override
            calls.append(plan.to_dict())
            if len(calls) < 3:
                return []
            return [_track("third")]

    plan = jamendo.JamendoSearchPlan.from_dict(_plan_dict())
    candidates, effective_plan, fallbacks = jamendo._search_candidates_with_fallbacks(FakeClient("client-id"), plan)

    assert [item.id for item in candidates] == ["third"]
    assert effective_plan.strategies[0].name.endswith("_broad_relevance")
    assert len(calls) == 3
    assert [item["code"] for item in fallbacks] == [
        "jamendo_search_attempt_1",
        "jamendo_search_attempt_2_relaxed",
        "jamendo_search_attempt_3_broad",
    ]


def test_match_jamendo_music_relaxes_local_filters_when_candidates_are_overfiltered(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def search_tracks(self, plan, *, limit_override=None):
            del plan, limit_override
            return [_track("1", name="Calm Podcast", duration=999)]

    monkeypatch.setattr(jamendo, "JamendoClient", FakeClient)
    config = jamendo.JamendoConfig(
        client_id="client-id",
        download_dir=tmp_path / "downloads",
        metadata_dir=tmp_path / "metadata",
    )
    plan = jamendo.JamendoSearchPlan.from_dict(_plan_dict(excludeTerms=["podcast"], durationMin=60, durationMax=180))

    result = jamendo.match_jamendo_music(
        user_query="calm background",
        search_plan=plan,
        download=False,
        config=config,
    )

    assert result["candidateCount"] == 1
    assert result["fallbacks"][0]["code"] == "jamendo_filter_relaxed"


def test_filter_eligible_tracks_filters_download_false_and_duration_only_when_set():
    no_duration_plan = jamendo.JamendoSearchPlan.from_dict(_plan_dict(durationMin=None, durationMax=None))
    duration_plan = jamendo.JamendoSearchPlan.from_dict(_plan_dict(durationMin=60, durationMax=180))
    blocked = _track("1", audiodownload_allowed=False)
    long_track = _track("2", duration=999)
    ok = _track("3", duration=100)

    assert [item.id for item in jamendo.filter_eligible_tracks([blocked, long_track, ok], no_duration_plan)] == [
        "2",
        "3",
    ]
    assert [item.id for item in jamendo.filter_eligible_tracks([blocked, long_track, ok], duration_plan)] == ["3"]


def test_filter_eligible_tracks_dedupes_and_merges_source_strategies():
    plan = jamendo.JamendoSearchPlan.from_dict(_plan_dict())
    first = _track("1")
    second = _track("1")
    second.source_strategies = [
        jamendo.JamendoSearchStrategy.from_dict(_plan_dict(name="fallback")["strategies"][0]).to_dict()
    ]

    result = jamendo.filter_eligible_tracks([first, second], plan)

    assert len(result) == 1
    assert len(result[0].source_strategies) == 2
    assert "score" not in result[0].to_dict()


def test_filter_eligible_tracks_applies_hermes_exclude_terms_only():
    plan = jamendo.JamendoSearchPlan.from_dict(_plan_dict(excludeTerms=["podcast"]))
    keep_without_default_exclusions = _track("1", name="News Ambient")
    excluded_by_hermes = _track("2", name="Calm Podcast")

    result = jamendo.filter_eligible_tracks([keep_without_default_exclusions, excluded_by_hermes], plan)

    assert [item.id for item in result] == ["1"]


def test_sample_random_track_is_uniform_seeded_and_stable():
    candidates = [_track(str(index)) for index in range(5)]

    first, seed = jamendo.sample_random_track(candidates, seed=42)
    second, second_seed = jamendo.sample_random_track(list(reversed(candidates)), seed=42)
    other, _ = jamendo.sample_random_track(candidates, seed=1)

    assert first.id == second.id
    assert seed == second_seed == 42
    assert other.id in {item.id for item in candidates}


def test_sample_random_track_empty_pool_errors():
    with pytest.raises(jamendo.NoEligibleTracksError):
        jamendo.sample_random_track([], seed=42)


def test_download_uses_part_file_and_sanitizes_filename(tmp_path):
    track = _track("abc123", name="A/B:C*?", artist_name="Artist <>")
    client = jamendo.JamendoClient("client-id")
    calls = []

    def fake_download(url, part_path: Path):
        calls.append((url, part_path))
        assert part_path.name.endswith(".part")
        part_path.write_bytes(b"mp3")

    client._download_url = fake_download
    path = client.download_track_file(track, tmp_path)

    assert path.exists()
    assert path.read_bytes() == b"mp3"
    assert path.name.endswith("abc123.mp3")
    assert not any(char in path.name for char in '<>:"/\\|?*')
    assert calls[0][0] == track.audiodownload
    assert not (tmp_path / f"{path.name}.part").exists()


def test_download_rejects_not_allowed_without_part_file(tmp_path):
    track = _track("nope", audiodownload_allowed=False)
    client = jamendo.JamendoClient("client-id")

    with pytest.raises(CassetteError) as exc:
        client.download_track_file(track, tmp_path)

    assert exc.value.code == "jamendo_download_not_allowed"
    assert not list(tmp_path.glob("*.part"))


def test_tool_rejects_missing_fixed_form_without_prompt_leak(cassette_env):
    result = json.loads(tools.jamendo_music_matcher({"userQuery": "安静、未来感"}))
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["ok"] is False
    assert result["error"]["code"] == "jamendo_search_form_required"
    assert "你是 Jamendo 音乐搜索策略生成器" not in serialized
    assert "rawUserQuery" not in serialized
    assert "JAMENDO_CLIENT_SECRET" not in serialized


def test_tool_fixed_form_builds_safe_jamendo_plan(cassette_env, monkeypatch):
    observed = {}

    def fake_match(**kwargs):
        observed.update(kwargs)
        return {
            "status": "searched",
            "searchPlan": kwargs["search_plan"].to_dict(),
            "candidateCount": 0,
        }

    monkeypatch.setattr(tools.jamendo, "match_jamendo_music", fake_match)

    payload = json.loads(
        tools.jamendo_music_matcher(
            {
                "userQuery": "温柔男声流行歌",
                "searchTerms": ["gentle male vocal pop", "soft pop singer"],
                "fuzzyTags": ["pop", "gentle"],
                "excludeTerms": ["instrumental", "female"],
                "vocalInstrumental": "vocal",
                "download": False,
                "session_id": "fixed-form-session",
            }
        )
    )

    assert payload["ok"] is True
    plan = observed["search_plan"]
    assert plan.raw_user_query == "温柔男声流行歌"
    assert [strategy.search for strategy in plan.strategies] == [
        "gentle male vocal pop",
        "gentle male vocal pop",
        "gentle male vocal pop",
        "soft pop singer",
        "soft pop singer",
        "soft pop singer",
    ]
    assert all(strategy.tags == [] for strategy in plan.strategies)
    assert all(strategy.type == "single albumtrack" for strategy in plan.strategies)
    assert [strategy.order for strategy in plan.strategies] == [
        "relevance",
        "relevance",
        "popularity_total_desc",
        "relevance",
        "relevance",
        "popularity_total_desc",
    ]
    assert [strategy.boost for strategy in plan.strategies] == [
        "popularity_total",
        "downloads_total",
        None,
        "popularity_total",
        "downloads_total",
        None,
    ]
    assert all(strategy.vocalinstrumental == "vocal" for strategy in plan.strategies)


def test_tool_requires_client_id_when_executing_search(cassette_env, monkeypatch):
    monkeypatch.delenv("JAMENDO_CLIENT_ID", raising=False)
    payload = json.loads(
        tools.jamendo_music_matcher(
            {
                "userQuery": "安静、未来感",
                "searchPlan": _plan_dict(),
                "download": False,
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "jamendo_client_id_missing"
