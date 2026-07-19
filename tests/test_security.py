from __future__ import annotations


import pytest

from cassette.errors import CassetteError
from cassette import security


def test_allowed_path_passes(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"abc")
    assert security.resolve_and_validate_source_path(str(media)) == media.resolve()


def test_default_allowed_roots_include_gateway_cache_dirs(tmp_path, monkeypatch):
    monkeypatch.delenv("CASSETTE_ALLOWED_SOURCE_ROOTS", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    roots = security.get_allowed_source_roots()
    assert [path.name for path in roots] == ["weixin", "qqbot", "telegram", "cache", "tmp"]


def test_outside_path_rejected(cassette_env, tmp_path):
    media = tmp_path / "outside.mp4"
    media.write_bytes(b"abc")
    with pytest.raises(CassetteError) as exc:
        security.resolve_and_validate_source_path(str(media))
    assert exc.value.code == "source_path_outside_allowed_roots"


def test_symlink_escape_rejected(cassette_env, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"abc")
    link = cassette_env["source_root"] / "link.mp4"
    link.symlink_to(outside)
    with pytest.raises(CassetteError) as exc:
        security.resolve_and_validate_source_path(str(link))
    assert exc.value.code == "source_path_outside_allowed_roots"


def test_extension_and_size_limits(cassette_env):
    exe = cassette_env["source_root"] / "bad.exe"
    exe.write_bytes(b"abc")
    with pytest.raises(CassetteError) as exc:
        security.validate_extension(exe)
    assert exc.value.code == "disallowed_extension"

    big = cassette_env["source_root"] / "big.mp4"
    big.write_bytes(b"x" * 1025)
    with pytest.raises(CassetteError) as exc:
        security.validate_size(big)
    assert exc.value.code == "file_too_large"


def test_hash_stable(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"abc")
    assert security.sha256_file(media) == security.sha256_file(media)
    assert security.safe_hash_id("wxid_secret") == security.safe_hash_id("wxid_secret")
