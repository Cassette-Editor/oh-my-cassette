"""The contract's parsing rules, without any server."""

from __future__ import annotations

import copy
import re
from fractions import Fraction
from pathlib import Path

import pytest
from tests.reference_remote import PREPARE

from oh_my_cassette.contract import (
    EXTENSION_ID,
    META_DOWNLOADS,
    META_LOCAL_FILES,
    META_REQUEST_ID,
    ContractError,
    Convert,
    ServerContract,
    Transcode,
    download_specs,
    is_path_schema,
    local_files_spec,
    mime_accepted,
    pointer_get,
    pointer_set,
    preparation_for,
    prepare_spec,
    rendition_frame_rates,
    request_id_pointer,
    schema_at_pointer,
    server_contract,
    source_frame_rate,
    version_tuple,
)


def test_json_pointer_get_and_set_with_escapes():
    doc = {"a/b": {"m~n": [1, {"x": "y"}]}}
    assert pointer_get(doc, "/a~1b/m~0n/1/x") == (True, "y")
    assert pointer_get(doc, "/missing") == (False, None)
    pointer_set(doc, "/a~1b/m~0n/0", 2)
    assert doc["a/b"]["m~n"][0] == 2
    with pytest.raises(ContractError):
        pointer_set(doc, "/nope/deeper", 1)
    with pytest.raises(ContractError):
        pointer_get(doc, "no-leading-slash")


def test_local_files_spec_defaults_and_validation():
    spec = local_files_spec({META_LOCAL_FILES: {"pointers": ["/paths"]}})
    assert spec is not None and spec.pointers == ("/paths",) and "video/*" in spec.accept
    assert local_files_spec({}) is None
    assert local_files_spec(None) is None
    for bad in ({"pointers": []}, {"pointers": ["paths"]}, {"pointers": ["/p"], "accept": ["video"]}, "x"):
        with pytest.raises(ContractError):
            local_files_spec({META_LOCAL_FILES: bad})


def test_prepare_declaration_parses_the_agreed_vocabulary():
    spec = local_files_spec({META_LOCAL_FILES: {"pointers": ["/files"], "prepare": PREPARE}}).prepare
    assert spec is not None
    canonical, preview = spec.renditions
    assert (canonical.role, canonical.container, canonical.mime_type) == ("canonical", "mp4", "video/mp4")
    assert (canonical.video.max_long_edge, canonical.video.max_short_edge, canonical.video.crf) == (
        2560,
        1080,
        20,
    )
    assert (canonical.video.max_frame_rate, preview.video.max_frame_rate) == (60.0, 30.0)
    assert (preview.video.preset, preview.audio.bitrate) == ("fast", 128000)
    assert canonical.video.keyframe_interval_seconds == 2 and canonical.audio.sample_rate == 48000
    assert spec.variable_frame_rate.rates[:2] == (Fraction(24), Fraction(24000, 1001))
    assert spec.variable_frame_rate.fallback == Fraction(30)
    assert "wav" in spec.audio.accept and spec.audio.convert.extension == "flac" and spec.audio.intake == ()
    assert spec.image.convert.quality == 92 and spec.image.convert.mime_type == "image/jpeg"
    assert spec.image.intake == ("heic", "heif", "tif", "tiff") and "jfif" in spec.image.accept
    assert spec.digest == prepare_spec(copy.deepcopy(PREPARE)).digest


def test_prepare_ignores_unknown_keys_but_its_digest_tracks_the_declaration():
    grown = copy.deepcopy(PREPARE)
    grown["subtitles"] = {"convert": "srt"}
    grown["video"]["renditions"][0]["video"]["tune"] = "film"
    assert prepare_spec(grown).renditions == prepare_spec(PREPARE).renditions
    assert prepare_spec(grown).digest != prepare_spec(PREPARE).digest
    assert prepare_spec({}).renditions is None  # nothing declared: everything uploads unchanged


@pytest.mark.parametrize(
    "path, value",
    [
        (("video", "renditions", 0, "video", "crf"), "twenty"),
        (("video", "renditions", 0, "video", "crf"), True),
        (("video", "renditions", 0, "video", "codec"), "av1"),
        (("video", "renditions", 0, "video", "maxShortEdge"), 4000),
        (("video", "renditions", 0, "video", "preset"), "ludicrous"),
        (("video", "renditions", 1, "role"), "canonical"),
        (("video", "renditions", 1, "role"), "original"),
        (("video", "renditions"), []),
        (("video", "renditions", 0, "video", "maxFrameRate"), 0),
        (("video", "variableFrameRate", "rates"), []),
        (("video", "variableFrameRate", "fallback"), {"num": 30, "den": 0}),
        (("video", "variableFrameRate", "rates", 0), {"num": 24}),
        (("audio", "accept"), "wav"),
        (("image", "convertOnIntake"), "heic"),
        (("audio", "convert", "container"), "mp3"),
        (("image", "convert", "quality"), 101),
        (("image", "convert", "mimeType"), "jpeg"),
    ],
)
def test_malformed_prepare_declarations_are_contract_errors(path, value):
    broken = copy.deepcopy(PREPARE)
    node = broken
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ContractError):
        local_files_spec({META_LOCAL_FILES: {"pointers": ["/files"], "prepare": broken}})


@pytest.mark.parametrize(
    "path",
    [("video", "variableFrameRate"), ("video", "renditions", 0, "video", "maxFrameRate")],
)
def test_video_must_declare_its_frame_rate_rule(path):
    missing = copy.deepcopy(PREPARE)
    node = missing
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    with pytest.raises(ContractError, match=path[-1]):
        prepare_spec(missing)


def test_a_rendition_must_declare_its_audio_encoding():
    missing = copy.deepcopy(PREPARE)
    del missing["video"]["renditions"][0]["audio"]
    with pytest.raises(ContractError, match="audio"):
        prepare_spec(missing)


def test_what_happens_to_each_kind_of_file():
    spec = prepare_spec(PREPARE)
    assert isinstance(preparation_for(spec, "video/x-matroska", "a.mkv"), Transcode)
    assert isinstance(preparation_for(spec, "video/mp4", "a.mp4"), Transcode)  # video is always transcoded
    assert preparation_for(spec, "image/png", "a.PNG") is None
    assert preparation_for(spec, "audio/x-wav", "a.wav") is None
    for name in ("a.heic", "a.TIFF", "a.jfif", "a.apng"):  # the service takes these unchanged
        assert preparation_for(spec, "image/heic", name) is None, name
    ppm = preparation_for(spec, "image/x-portable-pixmap", "a.ppm")
    assert isinstance(ppm, Convert) and ppm.kind == "image" and ppm.conversion.extension == "jpg"
    aiff = preparation_for(spec, "audio/aiff", "a.aiff")
    assert isinstance(aiff, Convert) and aiff.conversion.container == "flac"
    assert preparation_for(None, "video/mp4", "a.mp4") is None
    assert preparation_for(prepare_spec({"image": PREPARE["image"]}), "video/mp4", "a.mp4") is None
    assert preparation_for(spec, "application/pdf", "a.pdf") is None


R = Fraction


@pytest.mark.parametrize(
    "real, average, rates",
    [
        (R(120), R(120), [R(60), R(30)]),  # every 2nd frame, then every 2nd again
        (R(100), R(100), [R(50), R(25)]),
        (R(90), R(90), [R(45), R(45, 2)]),
        (R(60), R(60), [R(60), R(30)]),
        (R(30000, 1001), R(30000, 1001), [R(30000, 1001), R(30000, 1001)]),
        (R(24), R(24), [R(24), R(24)]),
        (R(15), R(15), [R(15), R(15)]),
        (R(60000, 1001), R(59941, 1000), [R(60000, 1001), R(30000, 1001)]),  # constant within 0.02: exact
        (R(30), R(2997, 125), [R(24000, 1001)] * 2),  # variable: the declared rate nearest its average
        (R(60), R(2770, 100), [R(30000, 1001)] * 2),  # 27.7 is nearer 30000/1001 than 30 or 25
        (R(90), R(6003, 100), [R(60), R(30)]),  # 60.03 is nearer 60 than 60000/1001
        (R(30), R(49, 2), [R(24), R(24)]),  # 24.5: as near 24 as 25, and 24 comes first
        (R(30), R(1000, 100), [R(30), R(30)]),  # below the slowest rate: the fallback
        (None, None, [R(30), R(30)]),  # unknown: the fallback
        (R(25), None, [R(25), R(25)]),  # no average: the real rate is snapped
    ],
)
def test_rendition_frame_rates_follow_the_declared_rule(real, average, rates):
    plan = preparation_for(prepare_spec(PREPARE), "video/mp4", "a.mp4")
    assert rendition_frame_rates(plan, real, average) == rates


def test_a_variable_rate_snaps_to_the_nearest_declared_rate():
    variable = prepare_spec(PREPARE).variable_frame_rate
    assert source_frame_rate(variable, None, R(55)) == R(60000, 1001)
    assert source_frame_rate(variable, None, R(275, 10)) == R(30000, 1001)
    assert source_frame_rate(variable, None, R(2397, 100)) == R(24000, 1001)
    assert source_frame_rate(variable, None, R(2396, 100)) == R(24000, 1001)  # within 0.02 of the slowest
    assert source_frame_rate(variable, None, R(2395, 100)) == R(30)  # further below it


def test_request_id_pointer_names_one_argument():
    assert request_id_pointer({META_REQUEST_ID: "/client_request_id"}) == "/client_request_id"
    assert request_id_pointer({}) is None and request_id_pointer(None) is None
    for bad in ("", "client_request_id", 7):
        with pytest.raises(ContractError):
            request_id_pointer({META_REQUEST_ID: bad})


def test_download_specs_require_http_urls_and_names():
    specs = download_specs(
        {
            META_DOWNLOADS: [
                {"url": "https://x/y", "fileName": "a.mp4", "size": 3, "sha256": "AB", "pointer": "/file"}
            ]
        }
    )
    assert specs[0].sha256 == "ab" and specs[0].pointer == "/file"
    assert download_specs(None) == []
    for bad in (
        [{"url": "file:///etc/passwd", "fileName": "a"}],
        [{"url": "https://x"}],
        {"url": "https://x"},
    ):
        with pytest.raises(ContractError):
            download_specs({META_DOWNLOADS: bad})


def test_server_contract_reads_extensions():
    assert server_contract({EXTENSION_ID: {"version": 1}}) == ServerContract(True, 1, None)
    assert (
        server_contract({EXTENSION_ID: {"version": 1, "minBridgeVersion": "0.6"}}).min_bridge_version == "0.6"
    )
    assert server_contract({"another/extension": {}}) == ServerContract(False)
    assert server_contract(None) == ServerContract(False)


def test_incompatibility_rules():
    assert ServerContract(False).incompatibility("0.5.0") is None
    assert ServerContract(True, 1).incompatibility("0.5.0") is None
    assert "v2" in ServerContract(True, 2).incompatibility("0.5.0")
    assert ">= 0.6.0" in ServerContract(True, 1, "0.6.0").incompatibility("0.5.0")
    assert ServerContract(True, 1, "0.5.0").incompatibility("0.5.0") is None
    assert version_tuple("0.10.0") > version_tuple("0.9.9")


def test_path_schema_detection():
    schema = {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "one": {"type": "string"},
            "n": {"type": "integer"},
            "nested": {"type": "object", "properties": {"p": {"type": "string"}}},
        },
    }
    assert is_path_schema(schema_at_pointer(schema, "/paths"))
    assert is_path_schema(schema_at_pointer(schema, "/one"))
    assert is_path_schema(schema_at_pointer(schema, "/nested/p"))
    assert not is_path_schema(schema_at_pointer(schema, "/n"))
    assert not is_path_schema(schema_at_pointer(schema, "/absent"))


def test_mime_patterns():
    assert mime_accepted("video/mp4", ("video/*",))
    assert not mime_accepted("text/plain", ("video/*", "image/*"))
    assert mime_accepted("text/plain", ("*/*",))


def test_every_bridge_error_code_is_in_the_skill():
    root = Path(__file__).resolve().parents[1]
    source = "".join(path.read_text("utf-8") for path in (root / "src/oh_my_cassette").rglob("*.py"))
    used = set(re.findall(r'"(bridge\.[a-z_]+)"', source))
    skill = (root / "skills/cassette-video-edit/SKILL.md").read_text("utf-8")
    documented = set(re.findall(r"`(bridge\.[a-z_]+)`", skill))
    assert used and used == documented
