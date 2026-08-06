#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import install_plugin  # noqa: E402


def _check(name: str, status: str, message: str, **details) -> dict:
    return {"name": name, "status": status, "message": message, "details": details}


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False
        )
        return proc.returncode, _sanitize_text((proc.stdout or "").strip())
    except Exception as exc:
        return 127, type(exc).__name__


def _sanitize_text(value: str) -> str:
    text = value or ""
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<email>", text)
    text = re.sub(r"wxid_[A-Za-z0-9_-]+", "wxid_<redacted>", text)
    text = re.sub(r"(?i)(password|secret|token|client_secret)(\s*[=:]\s*)\S+", r"\1\2<redacted>", text)
    text = re.sub(r"(?<![A-Za-z0-9])[0-9]{8,}(?![A-Za-z0-9])", "<id>", text)
    return text


def _redacted_env_snapshot(values: dict[str, str]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for key, value in values.items():
        if key.endswith("PASSWORD") or key.endswith("SECRET") or key in {"CASSETTE_AUTH_EMAIL", "JAMENDO_CLIENT_ID"}:
            snapshot[key] = "<set>" if value else ""
        else:
            snapshot[key] = value
    return snapshot


def _read_plugin_version(plugin_dir: Path) -> str:
    manifest = plugin_dir / "plugin.yaml"
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"^version:\s*([0-9][\w.+-]*)", text, re.MULTILINE)
    return match.group(1) if match else ""


def _check_plugin(home: Path, repo: Path) -> dict:
    plugin_dir = home / "plugins" / "cassette"
    if not plugin_dir.exists() and not plugin_dir.is_symlink():
        return _check("plugin", "fail", f"plugin is not installed at {plugin_dir}")
    if plugin_dir.is_symlink():
        try:
            target = plugin_dir.resolve()
        except OSError:
            return _check("plugin", "fail", f"plugin symlink is broken: {plugin_dir}")
        if target == repo.resolve():
            return _check(
                "plugin", "ok", "plugin symlink points to this checkout", path=str(plugin_dir), target=str(target)
            )
        return _check(
            "plugin",
            "warn",
            "plugin symlink points to a different checkout",
            path=str(plugin_dir),
            target=str(target),
            expected=str(repo.resolve()),
        )
    try:
        resolved = plugin_dir.resolve()
    except OSError:
        resolved = plugin_dir
    if resolved == repo.resolve():
        return _check("plugin", "ok", "plugin directory is this checkout", path=str(plugin_dir))
    if (plugin_dir / ".git").exists():
        returncode, remote = _run(["git", "-C", str(plugin_dir), "remote", "get-url", "origin"])
        if returncode != 0:
            return _check(
                "plugin",
                "warn",
                "plugin directory is a git clone but its remote could not be read",
                path=str(plugin_dir),
                output=remote,
            )
        if "oh-my-cassette" not in remote:
            return _check(
                "plugin",
                "warn",
                "plugin directory is a git clone of a different repository",
                path=str(plugin_dir),
                remote=remote,
            )
        installed_version = _read_plugin_version(plugin_dir)
        local_version = _read_plugin_version(repo)
        if installed_version and local_version and installed_version != local_version:
            return _check(
                "plugin",
                "warn",
                f"installed plugin version {installed_version} differs from this checkout ({local_version}); run `hermes plugins update cassette`",
                path=str(plugin_dir),
                remote=remote,
            )
        return _check(
            "plugin",
            "ok",
            "plugin is a git clone managed by Hermes; update with `hermes plugins update cassette`",
            path=str(plugin_dir),
            remote=remote,
        )
    return _check(
        "plugin",
        "warn",
        "plugin directory exists but is neither a symlink nor a git clone; reinstall with `hermes plugins install Cassette-Editor/oh-my-cassette --force` or scripts/install_plugin.py",
        path=str(plugin_dir),
    )


def _check_plugin_enabled(home: Path) -> dict:
    python = install_plugin.hermes_python(home)
    if not python.exists():
        return _check("plugin_enabled", "fail", f"Hermes Python was not found: {python}")
    env = os.environ.copy()
    env.setdefault("HERMES_ACCEPT_HOOKS", "1")
    try:
        proc = subprocess.run(
            [str(python), "-m", "hermes_cli.main", "plugins", "list"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except Exception as exc:
        return _check("plugin_enabled", "fail", f"Hermes plugin list check failed: {type(exc).__name__}")

    output = _sanitize_text((proc.stdout or "").strip())
    if proc.returncode != 0:
        return _check("plugin_enabled", "fail", "Hermes plugin list command failed", output=output[-1000:])
    for line in output.splitlines():
        normalized = re.sub(r"\s+", " ", re.sub(r"[│┃|]", " ", line)).strip().lower()
        if not re.search(r"\bcassette\b", normalized):
            continue
        if "not enabled" in normalized:
            return _check(
                "plugin_enabled",
                "warn",
                "Cassette plugin is installed but not enabled; run `hermes plugins enable cassette`",
            )
        if re.search(r"\benabled\b", normalized):
            return _check("plugin_enabled", "ok", "Cassette plugin is enabled in Hermes")
    return _check(
        "plugin_enabled", "warn", "Cassette plugin was not found in `hermes plugins list` output", output=output[-1000:]
    )


_RESOLVED_ENV_KEYS = (
    "CASSETTE_AUTH_EMAIL",
    "CASSETTE_AUTH_PASSWORD",
    "CASSETTE_API_URL",
    "CASSETTE_FFMPEG_BIN",
    "CASSETTE_FFPROBE_BIN",
    "JAMENDO_CLIENT_ID",
)


def _resolved_env_values(home: Path) -> tuple[dict[str, str], list[str]]:
    """What the plugin will actually read, in the runtime's precedence order.

    Reading only ~/.hermes/.env made this report credentials missing during sessions that
    authenticated perfectly well: the process environment wins at runtime, so a value
    exported in the shell that launched Hermes is the one that counts. Returns the merged
    values and the keys the environment supplied, so the report can say which is which.
    """
    values = dict(install_plugin.read_env_values(home / ".env"))
    from_environment: list[str] = []
    for key in _RESOLVED_ENV_KEYS:
        override = (os.getenv(key) or "").strip()
        if override:
            values[key] = override
            from_environment.append(key)
    return values, from_environment


def _check_env(home: Path, values: dict[str, str], from_environment: list[str]) -> dict:
    env_path = home / ".env"
    # CASSETTE_API_URL is intentionally not required: it has a working default.
    missing = [key for key in ("CASSETTE_AUTH_EMAIL", "CASSETTE_AUTH_PASSWORD") if not values.get(key)]
    status = "ok" if not missing else "warn"
    if missing:
        message = f"missing values: {', '.join(missing)}"
    elif from_environment:
        message = (
            f"required Cassette environment values are present ({', '.join(from_environment)} from the environment)"
        )
    else:
        message = "required Cassette environment values are present"
    return _check(
        "env",
        status,
        message,
        path=str(env_path),
        values=_redacted_env_snapshot(values),
        from_environment=from_environment,
    )


def _check_binary(name: str, configured: str = "") -> dict:
    path = install_plugin._find_executable(name, configured)
    if not path:
        return _check(name, "fail", f"{name} was not found")
    code, output = _run([path, "-version"], timeout=10)
    if code != 0:
        return _check(name, "fail", f"{name} exists but did not run successfully", path=path, output=output[-500:])
    return _check(name, "ok", f"{name} is available", path=path, version=output.splitlines()[0] if output else "")


def _check_gateway(home: Path) -> dict:
    python = install_plugin.hermes_python(home)
    if not python.exists():
        return _check("gateway", "fail", f"Hermes Python was not found: {python}")
    env = os.environ.copy()
    env.setdefault("HERMES_ACCEPT_HOOKS", "1")
    try:
        proc = subprocess.run(
            [str(python), "-m", "hermes_cli.main", "gateway", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except Exception as exc:
        return _check("gateway", "fail", f"gateway status check failed: {type(exc).__name__}")
    status = "ok" if proc.returncode == 0 else "warn"
    return _check(
        "gateway",
        status,
        "gateway status command completed" if status == "ok" else "gateway status command reported a problem",
        output=_sanitize_text((proc.stdout or "").strip())[-1000:],
    )


def _check_cassette_connectivity(url: str) -> dict:
    """Probe the API origin the plugin actually calls, via its unauthenticated /healthz."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return _check("cassette_url", "warn", "Cassette API URL is not HTTP(S); connectivity check skipped", url=url)
    target = url.rstrip("/") + "/healthz"
    try:
        request = Request(target, method="GET", headers={"User-Agent": "oh-my-cassette-diagnose/1.0"})
        with urlopen(request, timeout=10) as response:
            status = int(getattr(response, "status", 200) or 200)
        if 200 <= status < 400:
            return _check("cassette_url", "ok", "Cassette API is reachable", url=url, http_status=status)
        return _check("cassette_url", "fail", "Cassette API returned an unhealthy status", url=url, http_status=status)
    except HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        if status and status < 500:
            return _check("cassette_url", "ok", "Cassette API is reachable", url=url, http_status=status)
        return _check("cassette_url", "fail", "Cassette API request failed", url=url, http_status=status)
    except (TimeoutError, URLError, OSError) as exc:
        return _check("cassette_url", "fail", "Cassette API is not reachable", url=url, error=type(exc).__name__)


def _check_cassette_login(home: Path, url: str, email: str, password: str) -> dict:
    """Verify credentials against the same endpoint the plugin authenticates with.

    ``home`` is unused now that this is a direct API call rather than a subprocess in the
    Hermes interpreter; it stays in the signature so callers and tests are unaffected.
    """
    if not email or not password:
        return _check(
            "cassette_login", "warn", "Cassette login credentials are not configured; login verification skipped"
        )
    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    request = Request(
        url.rstrip("/") + "/api/agent-auth/verify",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            status = int(getattr(response, "status", 200) or 200)
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if int(exc.code) in {400, 401, 403}:
            return _check("cassette_login", "fail", "Cassette rejected the credentials", http_status=int(exc.code))
        return _check("cassette_login", "fail", "Cassette credential verification failed", http_status=int(exc.code))
    except (TimeoutError, URLError, OSError) as exc:
        return _check(
            "cassette_login",
            "fail",
            "Cassette credential verification could not reach the API",
            error=type(exc).__name__,
        )
    except ValueError:
        return _check("cassette_login", "fail", "Cassette credential verification returned invalid JSON")

    session = payload.get("session") if isinstance(payload, dict) else {}
    if status != 200 or not isinstance(session, dict) or not session.get("access_token"):
        return _check("cassette_login", "fail", "Cassette rejected the credentials", http_status=status)
    # Only whether a token was issued is recorded, never the token. The response also carries
    # an access level; it is not reported, because it does not describe anything the plugin
    # does — every operation here is an agent operation.
    return _check("cassette_login", "ok", "Cassette login credentials were accepted")


def diagnose(home: Path, repo: Path) -> list[dict]:
    env_values, from_environment = _resolved_env_values(home)
    url = env_values.get("CASSETTE_API_URL") or install_plugin.CASSETTE_DEFAULT_API_URL
    return [
        _check_plugin(home, repo),
        _check_plugin_enabled(home),
        _check_env(home, env_values, from_environment),
        _check_binary("ffmpeg", env_values.get("CASSETTE_FFMPEG_BIN", "")),
        _check_binary("ffprobe", env_values.get("CASSETTE_FFPROBE_BIN", "")),
        _check_cassette_connectivity(url),
        _check_cassette_login(
            home,
            url,
            env_values.get("CASSETTE_AUTH_EMAIL", ""),
            env_values.get("CASSETTE_AUTH_PASSWORD", ""),
        ),
        _check_gateway(home),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose Cassette Hermes plugin installation issues.")
    parser.add_argument("--hermes-home", help="Hermes home directory; defaults to $HERMES_HOME or ~/.hermes")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()

    home = install_plugin.hermes_home(args.hermes_home)
    repo = install_plugin.repo_root()
    checks = diagnose(home, repo)
    if args.json:
        print(json.dumps({"hermes_home": str(home), "checks": checks}, ensure_ascii=False, indent=2))
    else:
        print(f"Hermes home: {home}")
        for item in checks:
            print(f"[{item['status'].upper()}] {item['name']}: {item['message']}")
            details = item.get("details") or {}
            for key in ("path", "target", "python", "url", "http_status", "code", "version", "output"):
                if key in details and details[key]:
                    print(f"  {key}: {details[key]}")
    return 1 if any(item["status"] == "fail" for item in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
