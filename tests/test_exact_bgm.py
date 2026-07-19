from __future__ import annotations

import json
from pathlib import Path

import pytest

from cassette import exact_bgm, manifest


def _candidate(
    source: str,
    track_id: str,
    title: str,
    artist: str,
    *,
    query: str = "",
    display_index: int = 1,
    audio_url: str = "https://music.test/song.mp3",
) -> exact_bgm.ExactBgmCandidate:
    return exact_bgm.ExactBgmCandidate(
        provider="musicsquare_exact",
        source=source,
        id=track_id,
        title=title,
        artist=artist,
        audio_url=audio_url,
        query=query,
        display_index=display_index,
        raw={"id": track_id},
        source_strategies=[{"source": source, "query": query, "mode": "test"}],
    )


def test_exact_search_falls_back_to_title_only_when_full_query_has_no_results(cassette_env):
    class FakeClient(exact_bgm.ExactBgmClient):
        def __init__(self):
            self.calls = []

        def search_all(self, query, *, limit=None):
            self.calls.append((query, limit))
            if query == "夜空中最亮的星":
                return [_candidate("qq", "1", "夜空中最亮的星", "逃跑计划", query=query)]
            return []

        def ensure_audio_url(self, candidate):
            return candidate

    client = FakeClient()

    candidates, selected, attempts = exact_bgm.search_exact_song(
        client,
        title="夜空中最亮的星",
        artist="错误歌手",
        limit=10,
    )

    assert client.calls == [("夜空中最亮的星 错误歌手", 10), ("夜空中最亮的星", 10)]
    assert selected.title == "夜空中最亮的星"
    assert selected.artist == "逃跑计划"
    assert attempts[0]["eligible_count"] == 0
    assert attempts[1]["eligible_count"] == 1
    assert len(candidates) == 1


def test_exact_search_deterministically_chooses_best_matching_candidate(cassette_env):
    class FakeClient(exact_bgm.ExactBgmClient):
        def search_all(self, query, *, limit=None):
            del query, limit
            return [
                _candidate("kuwo", "3", "Song A", "Artist A", display_index=1),
                _candidate("netease", "1", "Song A", "Artist A", display_index=3),
                _candidate("qq", "2", "Song A Live", "Artist A", display_index=1),
            ]

        def ensure_audio_url(self, candidate):
            return candidate

    _, selected, _ = exact_bgm.search_exact_song(FakeClient(), title="Song A", artist="Artist A")

    assert selected.source == "netease"
    assert selected.id == "1"


def test_exact_search_skips_candidate_with_invalid_audio_url(cassette_env):
    class FakeClient(exact_bgm.ExactBgmClient):
        def search_all(self, query, *, limit=None):
            del limit
            return [
                _candidate("qq", "bad", "Song A", "Artist A", query=query, audio_url="None"),
                _candidate("qq", "good", "Song A", "Artist A", query=query, audio_url="https://music.test/good.mp3"),
            ]

        def ensure_audio_url(self, candidate):
            return candidate

    _, selected, attempts = exact_bgm.search_exact_song(FakeClient(), title="Song A", artist="Artist A")

    assert selected.id == "good"
    assert attempts[0]["candidate_failures"][0]["track_id"] == "bad"
    assert attempts[0]["candidate_failures"][0]["code"] == "exact_bgm_audio_url_missing"
    assert attempts[0]["candidate_failures"][0]["audio_url"] == {"status": "invalid_literal", "value": "none"}


def test_match_exact_bgm_retries_next_candidate_after_download_url_failure(cassette_env):
    class FakeClient(exact_bgm.ExactBgmClient):
        def search_all(self, query, *, limit=None):
            del limit
            return [
                _candidate("qq", "bad", "晴天", "周杰伦", query=query, audio_url="https://music.test/bad.mp3"),
                _candidate("qq", "good", "晴天", "周杰伦", query=query, audio_url="https://music.test/good.mp3"),
            ]

        def ensure_audio_url(self, candidate):
            return candidate

        def download_candidate(self, candidate, output_dir):
            if candidate.id == "bad":
                raise exact_bgm.CassetteError(
                    "exact_bgm_invalid_audio_url",
                    "Exact BGM download URL was not a valid http(s) URL",
                    {"url": {"status": "invalid_literal", "value": "none"}},
                )
            output_dir = exact_bgm._safe_output_dir(output_dir)
            path = output_dir / "jay-chou-good.mp3"
            path.write_bytes(b"fake mp3")
            return path

    result = exact_bgm.match_exact_bgm(
        session_id="exact-session",
        instruction="剪成温柔怀旧短片",
        title="晴天",
        artist="周杰伦",
        config=exact_bgm.ExactBgmConfig(),
        client=FakeClient(),
    )

    assert result["status"] == "downloaded"
    assert result["track_id"] == "good"
    assert result["downloadFailures"][0]["track_id"] == "bad"
    assert result["attempts"][0]["candidate_failures"][0]["code"] == "exact_bgm_invalid_audio_url"


def test_exact_title_matching_ignores_chinese_song_wrappers(cassette_env):
    candidates = [
        _candidate("qq", "mid-1", "New Boy", "房东的猫", query="《New Boy》 房东的猫"),
    ]

    eligible = exact_bgm.filter_exact_candidates(
        candidates,
        title="《New Boy》",
        artist="房东的猫",
        require_artist=True,
        strict_title=True,
    )

    assert len(eligible) == 1
    assert eligible[0].title == "New Boy"


def test_exact_search_rejects_partial_title_when_artist_query_matches(cassette_env):
    class FakeClient(exact_bgm.ExactBgmClient):
        def __init__(self):
            self.calls = []

        def search_all(self, query, *, limit=None):
            self.calls.append((query, limit))
            if query == "My Love Ace Spectrum":
                return [_candidate("netease", "wrong", "Me And My Love", "Ace Spectrum", query=query)]
            return []

        def ensure_audio_url(self, candidate):
            return candidate

    client = FakeClient()

    with pytest.raises(exact_bgm.CassetteError) as exc:
        exact_bgm.search_exact_song(client, title="My Love", artist="Ace Spectrum")

    assert exc.value.code == "exact_bgm_no_search_results"
    assert client.calls == [("My Love Ace Spectrum", 10), ("My Love", 10)]
    attempts = exc.value.details["attempts"]
    assert attempts[0]["candidate_count"] == 1
    assert attempts[0]["eligible_count"] == 0
    assert attempts[0]["strict_title"] is True


def test_exact_client_continues_after_one_source_failure(cassette_env):
    class FakeClient(exact_bgm.ExactBgmClient):
        def __init__(self):
            super().__init__(exact_bgm.ExactBgmConfig(sources=("netease", "qq")))

        def search_netease(self, query, limit):
            del query, limit
            raise exact_bgm.CassetteError("exact_bgm_http_error", "netease down")

        def search_qq(self, query, limit):
            del limit
            return [_candidate("qq", "2", "Song A", "Artist A", query=query)]

    candidates = FakeClient().search_all("Song A Artist A", limit=10)

    assert [(item.source, item.id) for item in candidates] == [("qq", "2")]


def test_match_exact_bgm_downloads_registers_asset_and_saves_metadata(cassette_env):
    class FakeClient(exact_bgm.ExactBgmClient):
        def search_all(self, query, *, limit=None):
            del limit
            return [_candidate("qq", "mid-1", "晴天", "周杰伦", query=query)]

        def ensure_audio_url(self, candidate):
            return candidate

        def download_candidate(self, candidate, output_dir):
            output_dir = exact_bgm._safe_output_dir(output_dir)
            path = output_dir / "jay-chou-sunny.mp3"
            path.write_bytes(b"fake mp3")
            return path

    result = exact_bgm.match_exact_bgm(
        session_id="exact-session",
        instruction="剪成温柔怀旧短片",
        title="晴天",
        artist="周杰伦",
        config=exact_bgm.ExactBgmConfig(),
        client=FakeClient(),
    )

    assert result["status"] == "downloaded"
    assert result["provider"] == "musicsquare_exact"
    assert result["source"] == "qq"
    assert result["artist"] == "周杰伦"
    assert result["title"] == "晴天"
    assert result["file_path"].startswith("downloads/exact_bgm/")
    assert result["metadata_path"].startswith("metadata/exact_bgm/")

    metadata = json.loads((Path(cassette_env["asset_root"]) / result["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["provider"] == "musicsquare_exact"
    assert metadata["requestedTitle"] == "晴天"
    assert "CASSETTE_EXACT_BGM_JOOX_TOKEN" not in json.dumps(metadata, ensure_ascii=False)

    listed = manifest.list_assets(session_id="exact-session")["manifest"]
    audio_assets = [asset for asset in listed["assets"] if asset.get("media_type") == "audio"]
    assert len(audio_assets) == 1
    assert audio_assets[0]["metadata"]["source"] == "musicsquare_exact"


def test_exact_search_raises_when_no_title_or_title_only_results(cassette_env):
    class FakeClient(exact_bgm.ExactBgmClient):
        def search_all(self, query, *, limit=None):
            del query, limit
            return []

    with pytest.raises(exact_bgm.CassetteError) as exc:
        exact_bgm.search_exact_song(FakeClient(), title="Missing Song", artist="Missing Artist")

    assert exc.value.code == "exact_bgm_no_search_results"


def test_exact_bgm_filename_sanitizer_handles_special_characters():
    assert exact_bgm._safe_music_filename("A/B:C", "Song?*Name", "qq:mid", ".mp3") == "A_B_C - Song_Name - qq_mid.mp3"


def test_download_url_rejects_invalid_literal_without_value_error(cassette_env, tmp_path):
    client = exact_bgm.ExactBgmClient(exact_bgm.ExactBgmConfig())

    with pytest.raises(exact_bgm.CassetteError) as exc:
        client._download_url("None", tmp_path / "song.mp3.part", seen_urls=set())

    assert exc.value.code == "exact_bgm_invalid_audio_url"
    assert exc.value.details["url"] == {"status": "invalid_literal", "value": "none"}
