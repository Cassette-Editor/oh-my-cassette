from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

import mcp_plugin
from cassette import register


ROOT = Path(__file__).resolve().parents[1]

RELEASE_CHANNEL_REF = "release"


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text("utf-8"))


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("local_mcp_bootstrap", ROOT / "scripts" / "local_mcp_bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _declared_pins(name: str) -> dict[str, str]:
    """Every `package==version` an .in file pins, following its `-r` includes."""
    pins: dict[str, str] = {}
    for line in (ROOT / name).read_text("utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if entry.startswith("-r "):
            pins.update(_declared_pins(entry[3:].strip()))
            continue
        package, separator, version = entry.partition("==")
        if separator:
            pins[package.strip()] = version.split(";")[0].strip()
    return pins


def _load_opencode_installer():
    spec = importlib.util.spec_from_file_location("install_opencode", ROOT / "scripts" / "install_opencode.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    # The installer defers its repo imports until a plugin tree is resolved, so
    # that the script also runs when piped straight from the network.
    module._load_repo_modules(ROOT)
    return module


def test_dual_manifests_and_marketplaces_have_matching_identity_and_version():
    codex = _json(".codex-plugin/plugin.json")
    claude = _json(".claude-plugin/plugin.json")
    claude_market = _json(".claude-plugin/marketplace.json")
    codex_market = _json(".agents/plugins/marketplace.json")
    hermes = yaml.safe_load((ROOT / "plugin.yaml").read_text("utf-8"))

    assert codex["name"] == claude["name"] == "oh-my-cassette"
    assert codex_market["name"] == claude_market["name"] == "cassette-editor"
    assert codex_market["plugins"][0]["name"] == claude_market["plugins"][0]["name"] == codex["name"]
    # Every version-bearing file, not a subset: Claude and Codex both key their plugin
    # cache on the manifest version, so one file left behind on a release means those
    # users silently never receive the update. version.txt is what the runtime's
    # staleness check compares against the release channel.
    versions = {
        codex["version"],
        claude["version"],
        claude_market["plugins"][0]["version"],
        hermes["version"],
        (ROOT / "version.txt").read_text("utf-8").strip(),
        mcp_plugin.__version__,
        _json("server.json")["version"],
    }
    assert len(versions) == 1
    assert re.fullmatch(r"\d+\.\d+\.\d+", versions.pop())


def test_registry_manifest_fits_the_mcp_registry_limits():
    server = _json("server.json")
    # The namespace is what the registry authenticates against the GitHub org, and it
    # is case-sensitive: lowercasing the org is a 403 at publish time, not a redirect.
    # The 100-char description cap is the other quiet one — an overflow only fails at
    # publish time, long after the edit that caused it.
    assert server["name"] == "io.github.Cassette-Editor/oh-my-cassette"
    assert server["repository"]["url"] == "https://github.com/Cassette-Editor/oh-my-cassette"
    assert 0 < len(server["description"]) <= 100


def test_host_configs_use_one_stdio_server_and_no_network_listener():
    assert _json(".claude-plugin/plugin.json")["mcpServers"] == "./.claude-plugin/mcp.json"
    assert _json(".codex-plugin/plugin.json")["mcpServers"] == "./.codex-plugin/mcp.json"
    codex = _json(".codex-plugin/mcp.json")["mcpServers"]
    # Claude external MCP files are the server map itself, not the generic
    # project-level {"mcpServers": ...} wrapper.
    claude = _json(".claude-plugin/mcp.json")
    project = _json(".mcp.json")["mcpServers"]
    assert set(codex) == set(claude) == set(project) == {"cassette"}
    for config in (codex["cassette"], claude["cassette"], project["cassette"]):
        assert config["command"] == "python3"
        assert any("run_local_mcp.py" in item for item in config["args"])
        assert "url" not in config and "port" not in config
    assert "cwd" not in codex["cassette"]
    assert {"CODEX_HOME", "CASSETTE_CONFIG_HOME", "CASSETTE_DATA_HOME"} <= set(codex["cassette"]["env_vars"])
    assert "${CLAUDE_PLUGIN_ROOT}" in claude["cassette"]["args"][0]
    assert claude["cassette"]["env"]["CASSETTE_PROJECT_ROOT"] == "${CLAUDE_PROJECT_DIR}"


def test_repo_root_mcp_config_is_claude_project_scoped_not_codex():
    # Claude Code auto-loads a repo-root .mcp.json for everyone who opens this
    # checkout, so it must be the Claude project shape and must never contain
    # the Codex cache-glob launcher or Codex-only fields (the exact regression
    # that made the MCP fail inside Claude).
    project = _json(".mcp.json")["mcpServers"]["cassette"]
    assert "CODEX_HOME" not in json.dumps(project)
    # "timeout" belongs here too: Claude Code reads it as a HARD wall-clock cap that
    # progress notifications do not extend, so shipping one would cap every blocking
    # run_job at a number chosen without knowing the edit.
    for codex_only in ("env_vars", "startup_timeout_sec", "tool_timeout_sec", "timeout"):
        assert codex_only not in project
    assert project["args"] == ["${CLAUDE_PROJECT_DIR:-.}/scripts/run_local_mcp.py"]


def test_hermes_loads_the_host_neutral_skill_and_keeps_only_a_gateway_addendum():
    neutral = (ROOT / "skills" / "cassette-video-edit" / "SKILL.md").read_text("utf-8")
    hermes = (ROOT / "hermes" / "skills" / "cassette-video-edit" / "SKILL.md").read_text("utf-8")
    assert "supported MCP host" in neutral
    assert "gateway user" not in neutral
    assert "Hermes" in hermes

    class Context:
        def __init__(self):
            self.skills = []

        def register_tool(self, **_kwargs):
            pass

        def register_command(self, *_args, **_kwargs):
            pass

        def register_hook(self, *_args, **_kwargs):
            pass

        def register_skill(self, name, path, description=""):
            self.skills.append((name, Path(path), description))

    context = Context()
    register(context)
    assert context.skills[0][1] == ROOT / "skills" / "cassette-video-edit" / "SKILL.md"
    assert context.skills[1][1] == ROOT / "hermes" / "skills" / "cassette-video-edit" / "SKILL.md"


def test_release_please_updates_all_host_version_fields():
    config = _json("release-please-config.json")
    entries = config["packages"]["."]["extra-files"]
    entries_by_path = {entry["path"]: entry for entry in entries}
    assert {
        "plugin.yaml",
        ".codex-plugin/plugin.json",
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        "mcp_plugin/__init__.py",
        "server.json",
    } <= entries_by_path.keys()
    assert entries_by_path["plugin.yaml"] == {
        "type": "generic",
        "path": "plugin.yaml",
    }


def test_public_demo_docs_use_the_named_https_route():
    expected = "https://trycassette.online/agent-demo"
    for relative in ("README.md", "README.zh-cn.md", "llms.txt"):
        text = (ROOT / relative).read_text("utf-8")
        assert expected in text
        assert not re.search(r"http://(?:\d{1,3}\.){3}\d{1,3}:8080/?", text)


def test_codex_interface_assets_resolve_and_its_policy_pages_exist():
    # Marketplaces render this block, and a directory scanner treats the whole
    # group as one all-or-nothing check: a single dangling path or non-HTTPS URL
    # silently drops the plugin's listing metadata.
    interface = _json(".codex-plugin/plugin.json")["interface"]

    assets = [interface["logo"], interface["composerIcon"], *interface["screenshots"]]
    assert assets, "interface should declare renderable assets"
    for relative in assets:
        # The dot prefix is not cosmetic: the marketplace path validator rejects a
        # bare "assets/x.png" outright, the same way it already requires "./skills/".
        assert relative.startswith("./"), relative
        assert ".." not in Path(relative).parts, relative
        assert (ROOT / relative).is_file(), f"interface points at missing {relative}"

    site = "https://cassette-editor.github.io/oh-my-cassette/"
    for field in ("websiteURL", "privacyPolicyURL", "termsOfServiceURL"):
        assert interface[field].startswith("https://"), field

    # Both policy URLs are served by this repository's own Pages site out of
    # docs/, so renaming or deleting a page has to break here rather than in a
    # marketplace listing nobody on this side is watching.
    for field in ("privacyPolicyURL", "termsOfServiceURL"):
        url = interface[field]
        assert url.startswith(site), f"{field} should be served from this repo's docs site"
        page = ROOT / "docs" / f"{url[len(site) :]}.md"
        assert page.is_file(), f"{field} points at missing {page.relative_to(ROOT)}"


def test_every_shipped_skill_declares_its_license():
    for skill in sorted(ROOT.glob("skills/*/SKILL.md")) + sorted(ROOT.glob("hermes/skills/*/SKILL.md")):
        front_matter = skill.read_text("utf-8").split("---", 2)[1]
        assert yaml.safe_load(front_matter).get("license") == "MIT", skill.relative_to(ROOT)


def test_opencode_project_config_and_agents_skill_copies_stay_in_sync():
    config = _json("opencode.json")["mcp"]["cassette"]
    assert config["type"] == "local"
    assert config["command"] == ["python3", "scripts/run_local_mcp.py"]
    assert config["environment"]["CASSETTE_MCP_HOST"] == "opencode"

    def _tree(root: Path) -> dict[str, str]:
        return {
            str(path.relative_to(root)): path.read_text("utf-8") for path in sorted(root.rglob("*")) if path.is_file()
        }

    for skill_name in ("cassette-video-edit", "cassette-model"):
        neutral_root = ROOT / "skills" / skill_name
        agents_root = ROOT / ".agents" / "skills" / skill_name
        assert _tree(agents_root) == _tree(neutral_root)

    # Every references/… path the body names has to resolve, on every host that
    # installs the skill by copying this directory.
    neutral_root = ROOT / "skills" / "cassette-video-edit"
    body = (neutral_root / "SKILL.md").read_text("utf-8")
    referenced = set(re.findall(r"`(references/[\w./-]+\.md)`", body))
    assert referenced, "SKILL.md should point at its bundled references"
    for relative in sorted(referenced):
        assert (neutral_root / relative).is_file(), f"SKILL.md points at missing {relative}"


def test_codex_and_claude_install_from_the_release_channel_not_main():
    # Both hosts clone the plugin tree themselves, so an unpinned ref silently
    # ships main-HEAD under whatever version the manifests happen to declare.
    codex_source = _json(".agents/plugins/marketplace.json")["plugins"][0]["source"]
    assert codex_source["source"] == "url"
    assert codex_source["ref"] == RELEASE_CHANNEL_REF

    claude_source = _json(".claude-plugin/marketplace.json")["plugins"][0]["source"]
    # A relative "./" source would resolve against the marketplace clone, which
    # tracks the default branch — it cannot be pinned.
    assert isinstance(claude_source, dict), "relative plugin sources cannot pin a ref"
    assert claude_source["source"] == "github"
    assert claude_source["repo"] == "Cassette-Editor/oh-my-cassette"
    assert claude_source["ref"] == RELEASE_CHANNEL_REF


def test_release_workflow_fast_forwards_the_release_channel():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release-please.yml").read_text("utf-8"))
    steps = workflow["jobs"]["release-please"]["steps"]
    release_step = next(step for step in steps if str(step.get("uses", "")).startswith("googleapis/release-please"))
    assert release_step["id"] == "release", "the push step keys off this step id"

    pushes = [step for step in steps if RELEASE_CHANNEL_REF in str(step.get("run", ""))]
    assert pushes, "no step advances the release channel; Codex/Claude would pin to a stale tag forever"
    for step in pushes:
        assert "steps.release.outputs.release_created" in str(step.get("if", ""))
    assert workflow["permissions"]["contents"] == "write"


def test_release_workflow_republishes_to_the_mcp_registry():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release-please.yml").read_text("utf-8"))
    steps = workflow["jobs"]["release-please"]["steps"]
    publish = next((step for step in steps if "mcp-publisher publish" in str(step.get("run", ""))), None)
    # release-please bumps server.json but cannot push it to the registry, so without
    # this step the registry entry silently freezes at the last hand-published version.
    assert publish, "no step republishes server.json; the registry would serve a stale version forever"
    assert "steps.release.outputs.release_created" in str(publish["if"])
    # OIDC is what keeps the publish secretless; it is inert without this permission.
    assert workflow["permissions"]["id-token"] == "write"
    assert "login github-oidc" in publish["run"]


def test_native_smoke_install_rewrites_both_pinned_marketplace_sources():
    # Both marketplaces pin the plugin to the release channel, so a verbatim
    # install fetches a published tag. Without a rewrite the smoke jobs would
    # silently stop testing the checkout and still report green.
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    steps = yaml.safe_load(ci)["jobs"]["native-packaging"]["steps"]
    scripts = {str(step.get("name", "")): str(step.get("run", "")) for step in steps}

    codex = next(body for name, body in scripts.items() if "Codex" in name and "Smoke-install" in name)
    assert ".agents/plugins/marketplace.json" in codex
    assert '"source":"local"' in codex.replace(" ", "")

    claude = next(body for name, body in scripts.items() if "Claude" in name and "Smoke-install" in name)
    assert ".claude-plugin/marketplace.json" in claude
    assert '.plugins[0].source="./"' in claude


def test_opencode_installer_entry_matches_the_project_config_contract():
    installer = _load_opencode_installer()
    entry = installer.mcp_server_entry(ROOT)
    project = _json("opencode.json")["mcp"]["cassette"]

    assert entry["type"] == project["type"]
    assert entry["environment"] == project["environment"]
    assert entry["enabled"] is True
    # The project config uses a repo-relative launcher; the installed one is
    # absolute, so only the tail is comparable across the two.
    assert entry["command"][-1] == str(ROOT / "scripts" / "run_local_mcp.py")
    assert Path(entry["command"][-1]).name == Path(project["command"][-1]).name


def test_opencode_installer_merge_preserves_unrelated_user_config():
    installer = _load_opencode_installer()
    existing = {
        "$schema": "https://opencode.ai/config.json",
        "theme": "dark",
        "model": "anthropic/claude-opus-5",
        "mcp": {"jira": {"type": "remote", "url": "https://jira.example.com/mcp"}},
    }
    merged = installer.merge_server(existing, installer.mcp_server_entry(ROOT))

    assert merged["theme"] == "dark"
    assert merged["model"] == "anthropic/claude-opus-5"
    assert merged["mcp"]["jira"] == existing["mcp"]["jira"]
    assert merged["mcp"]["cassette"]["environment"]["CASSETTE_MCP_HOST"] == "opencode"
    # The caller's dicts must not be mutated in place.
    assert set(existing["mcp"]) == {"jira"}


def test_opencode_installer_merge_seeds_an_empty_config():
    installer = _load_opencode_installer()
    merged = installer.merge_server({}, installer.mcp_server_entry(ROOT))
    assert merged["$schema"] == installer.OPENCODE_SCHEMA
    assert set(merged["mcp"]) == {"cassette"}


def test_opencode_installer_targets_an_existing_jsonc_config(tmp_path, monkeypatch):
    # opencode reads opencode.json OR opencode.jsonc. Writing the .json variant
    # blindly would leave a second, competing config beside the user's .jsonc.
    installer = _load_opencode_installer()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)

    home = tmp_path / "opencode"
    home.mkdir(parents=True)
    assert installer.opencode_config_path().name == "opencode.json"

    (home / "opencode.jsonc").write_text("{}", encoding="utf-8")
    assert installer.opencode_config_path().name == "opencode.jsonc"


def test_opencode_installer_honors_the_opencode_config_env_var(tmp_path, monkeypatch):
    installer = _load_opencode_installer()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    custom = tmp_path / "elsewhere" / "custom.json"
    monkeypatch.setenv("OPENCODE_CONFIG", str(custom))

    assert installer.opencode_config_path() == custom.resolve()
    # Skills are only discovered under the global directory, so a redirected
    # config file must not drag the skill install with it.
    assert installer.skill_destination() == (tmp_path / "opencode" / "skills" / "cassette-video-edit" / "SKILL.md")
    assert installer.skill_destination("cassette-model") == (
        tmp_path / "opencode" / "skills" / "cassette-model" / "SKILL.md"
    )
    assert installer.command_destination() == tmp_path / "opencode" / "commands" / "cassette-model.md"


def test_opencode_installer_ships_the_bundled_references_not_just_skill_md(tmp_path):
    # opencode discovers skills as directories. Copying SKILL.md alone would install a
    # body whose references/ pointers resolve to nothing, and the reader would lose the
    # sign-in recovery steps without any error to notice.
    installer = _load_opencode_installer()
    dest = tmp_path / "opencode" / "skills" / "cassette-video-edit" / "SKILL.md"

    installer.install_skill(ROOT / "skills" / "cassette-video-edit", dest, dry_run=False)

    assert dest.is_file()
    for relative in ("references/auth.md", "references/previews.md"):
        assert (dest.parent / relative).is_file(), f"installer dropped {relative}"

    # Re-running the installer is how a user updates, so it has to replace cleanly
    # rather than leave a stale file behind from the previous layout.
    stale = dest.parent / "references" / "removed.md"
    stale.write_text("stale", encoding="utf-8")
    installer.install_skill(ROOT / "skills" / "cassette-video-edit", dest, dry_run=False)
    assert not stale.exists()


def test_opencode_installer_ships_model_skill_and_slash_command(tmp_path):
    installer = _load_opencode_installer()
    skill_dest = tmp_path / "opencode" / "skills" / "cassette-model" / "SKILL.md"
    command_dest = tmp_path / "opencode" / "commands" / "cassette-model.md"

    installer.install_skill(ROOT / "skills" / "cassette-model", skill_dest, dry_run=False)
    installer.install_command(ROOT / "opencode" / "commands" / "cassette-model.md", command_dest, dry_run=False)

    assert skill_dest.is_file()
    assert command_dest.is_file()
    assert "cassette_config" in command_dest.read_text("utf-8")


def test_opencode_installer_refuses_to_rewrite_a_jsonc_config(tmp_path):
    # Round-tripping through json.dumps would silently delete the comments.
    installer = _load_opencode_installer()
    config = tmp_path / "opencode.jsonc"
    original = '{\n  // my notes\n  "theme": "dark"\n}\n'
    config.write_text(original, encoding="utf-8")

    with pytest.raises(installer.ManualStep, match="JSONC"):
        installer.read_config(config)
    assert config.read_text(encoding="utf-8") == original


def _tar_with(tmp_path, names, *, symlink_to=None):
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = 0
            archive.addfile(info, io.BytesIO(b""))
        if symlink_to:
            link = tarfile.TarInfo(symlink_to[0])
            link.type = tarfile.SYMTYPE
            link.linkname = symlink_to[1]
            archive.addfile(link)
    buffer.seek(0)
    return tarfile.open(fileobj=buffer, mode="r:gz")


def test_opencode_installer_rejects_path_traversal_in_the_downloaded_archive(tmp_path):
    # The tarball comes off the network, so a traversal member must never land
    # outside the destination.
    installer = _load_opencode_installer()
    dest = tmp_path / "unpack"
    dest.mkdir()
    with _tar_with(tmp_path, ["repo/ok.txt", "../escaped.txt"]) as archive:
        with pytest.raises(SystemExit, match="outside the destination"):
            installer._safe_extract(archive, dest)
    assert not (tmp_path / "escaped.txt").exists()


def test_opencode_installer_archive_must_have_a_single_root(tmp_path):
    installer = _load_opencode_installer()
    dest = tmp_path / "unpack"
    dest.mkdir()
    with _tar_with(tmp_path, ["one/a.txt", "two/b.txt"]) as archive:
        with pytest.raises(SystemExit, match="unexpected archive layout"):
            installer._safe_extract(archive, dest)


def test_opencode_installer_drops_link_members_and_returns_the_root(tmp_path):
    installer = _load_opencode_installer()
    dest = tmp_path / "unpack"
    dest.mkdir()
    with _tar_with(tmp_path, ["repo/a.txt"], symlink_to=("repo/evil", "/etc/passwd")) as archive:
        assert installer._safe_extract(archive, dest) == "repo"
    assert (dest / "repo" / "a.txt").exists()
    assert not (dest / "repo" / "evil").exists(), "symlink members must be skipped"


def test_opencode_installer_refuses_to_overwrite_a_git_checkout(tmp_path):
    # A tarball unpack over a developer's clone would destroy their work.
    installer = _load_opencode_installer()
    checkout = tmp_path / "clone"
    (checkout / ".git").mkdir(parents=True)
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        installer.download_plugin_tree(checkout, "release", dry_run=False)
    assert (checkout / ".git").exists()


def test_opencode_installer_prefers_its_own_checkout_over_downloading(monkeypatch):
    # Run from a real tree, the installer must register that tree and never
    # reach the network.
    installer = _load_opencode_installer()
    monkeypatch.setattr(
        installer, "download_plugin_tree", lambda *a, **k: pytest.fail("must not download from a checkout")
    )
    args = argparse.Namespace(plugin_dir=None, ref="release", dry_run=False)
    assert installer.resolve_plugin_root(args) == ROOT


def test_opencode_installer_sync_refuses_to_discard_uncommitted_changes(tmp_path, monkeypatch):
    # --sync runs `git reset --hard`; without this guard it eats local edits silently.
    installer = _load_opencode_installer()
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)

    monkeypatch.setattr(installer, "is_dirty", lambda _root: True)
    reset_calls = []
    monkeypatch.setattr(installer, "_git", lambda args, cwd: reset_calls.append(args))

    assert installer.sync_checkout(checkout, "release", dry_run=False) is False
    assert reset_calls == [], "a dirty checkout must never reach git reset --hard"

    # --force is the documented opt-in, and a clean checkout proceeds normally.
    monkeypatch.setattr(installer, "is_dirty", lambda _root: False)
    assert installer.sync_checkout(checkout, "release", dry_run=True) is True


def test_opencode_installer_reads_a_plain_json_config(tmp_path):
    installer = _load_opencode_installer()
    config = tmp_path / "opencode.json"
    config.write_text('{"theme": "dark"}', encoding="utf-8")
    assert installer.read_config(config) == {"theme": "dark"}
    assert installer.read_config(tmp_path / "missing.json") == {}


def test_every_documented_ruff_pin_names_the_version_ci_installs():
    # ruff's default rule set changes between minor releases: 0.16 reports 463 findings on
    # a tree 0.15.22 calls clean, so an unpinned `uvx ruff` buries a real finding under
    # phantom ones. Pinning it in several files recreates exactly the drift that put this
    # runtime a protocol revision behind, so the copies are checked rather than trusted.
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    installed = re.search(r"pip install ruff==(\d+\.\d+\.\d+)", ci)
    assert installed, "CI no longer installs a pinned ruff"
    version = installed.group(1)

    # required-version is the backstop: it makes a mismatched ruff refuse to run at all,
    # including from an editor integration that never reads the docs.
    pyproject = (ROOT / "pyproject.toml").read_text("utf-8")
    assert f'required-version = ">={version},<' in pyproject

    for document in (".github/PULL_REQUEST_TEMPLATE.md", "CONTRIBUTING.md"):
        documented = set(re.findall(r"uvx ruff@(\d+\.\d+\.\d+)", (ROOT / document).read_text("utf-8")))
        assert documented == {version}, f"{document} documents ruff {documented or 'unpinned'}, CI installs {version}"


@pytest.mark.parametrize(
    ("requirements_in", "lock"),
    [("requirements-mcp.in", "requirements-mcp.lock")],
)
def test_requirement_locks_carry_the_versions_their_inputs_declare(requirements_in, lock):
    # Dependabot edits the .in files and has no idea the uv-compiled locks exist, but the
    # bootstrap installs from the lock. A half-applied bump therefore ships the OLD
    # dependency while every file a reviewer reads claims the new one -- which is how the
    # runtime sat on mcp 1.12.4 (protocol 2025-06-18) after the .in already said 1.28.1,
    # silently negotiating a revision below what the hosts offered.
    bootstrap = _load_bootstrap()
    declared = _declared_pins(requirements_in)
    assert declared, f"{requirements_in} pins nothing, so this guard would pass vacuously"
    for package, version in declared.items():
        assert bootstrap.locked_version(ROOT / lock, package) == version, (
            f"{lock} does not pin {package}=={version}; re-run the uv pip compile command "
            f"recorded in the header of {lock}"
        )


def test_bootstrap_reads_locked_versions_rather_than_trusting_a_literal(tmp_path):
    # The marker this feeds is what diagnostics report as the installed runtime, so a
    # hardcoded version outlives the bump that invalidates it and reports a package that
    # was never installed.
    bootstrap = _load_bootstrap()
    lock = tmp_path / "sample.lock"
    lock.write_text(
        "# This file was autogenerated by uv\nMcp_Sample==1.28.1\n"
        "uvicorn==0.51.0 ; sys_platform != 'emscripten'\n    # via mcp\n",
        encoding="utf-8",
    )
    # PEP 503 name normalization, and a pin has to survive its PEP 508 environment marker.
    assert bootstrap.locked_version(lock, "mcp-sample") == "1.28.1"
    assert bootstrap.locked_version(lock, "uvicorn") == "0.51.0"
    # A "# via mcp" comment mentioning a package is not a pin, and a missing lock is not fatal.
    assert bootstrap.locked_version(lock, "mcp") is None
    assert bootstrap.locked_version(tmp_path / "missing.lock", "mcp") is None
