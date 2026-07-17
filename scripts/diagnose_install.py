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

import install_plugin


def _check(name: str, status: str, message: str, **details) -> dict:
    return {"name": name, "status": status, "message": message, "details": details}


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False)
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
            return _check("plugin", "ok", "plugin symlink points to this checkout", path=str(plugin_dir), target=str(target))
        return _check("plugin", "warn", "plugin symlink points to a different checkout", path=str(plugin_dir), target=str(target), expected=str(repo.resolve()))
    try:
        resolved = plugin_dir.resolve()
    except OSError:
        resolved = plugin_dir
    if resolved == repo.resolve():
        return _check("plugin", "ok", "plugin directory is this checkout", path=str(plugin_dir))
    if (plugin_dir / ".git").exists():
        returncode, remote = _run(["git", "-C", str(plugin_dir), "remote", "get-url", "origin"])
        if returncode != 0:
            return _check("plugin", "warn", "plugin directory is a git clone but its remote could not be read", path=str(plugin_dir), output=remote)
        if "oh-my-cassette" not in remote:
            return _check("plugin", "warn", "plugin directory is a git clone of a different repository", path=str(plugin_dir), remote=remote)
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
    return _check("plugin", "warn", "plugin directory exists but is neither a symlink nor a git clone; reinstall with `hermes plugins install Cassette-Editor/oh-my-cassette --force` or scripts/install_plugin.py", path=str(plugin_dir))


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
            return _check("plugin_enabled", "warn", "Cassette plugin is installed but not enabled; run `hermes plugins enable cassette`")
        if re.search(r"\benabled\b", normalized):
            return _check("plugin_enabled", "ok", "Cassette plugin is enabled in Hermes")
    return _check("plugin_enabled", "warn", "Cassette plugin was not found in `hermes plugins list` output", output=output[-1000:])


def _check_env(home: Path) -> dict:
    env_path = home / ".env"
    values = install_plugin.read_env_values(env_path)
    missing = [key for key in ("CASSETTE_URL", "CASSETTE_AUTH_EMAIL", "CASSETTE_AUTH_PASSWORD") if not values.get(key)]
    status = "ok" if not missing else "warn"
    message = "required Cassette environment values are present" if not missing else f"missing values: {', '.join(missing)}"
    return _check("env", status, message, path=str(env_path), values=_redacted_env_snapshot(values))


def _check_binary(name: str, configured: str = "") -> dict:
    path = install_plugin._find_executable(name, configured)
    if not path:
        return _check(name, "fail", f"{name} was not found")
    code, output = _run([path, "-version"], timeout=10)
    if code != 0:
        return _check(name, "fail", f"{name} exists but did not run successfully", path=path, output=output[-500:])
    return _check(name, "ok", f"{name} is available", path=path, version=output.splitlines()[0] if output else "")


def _check_playwright(home: Path) -> dict:
    python = install_plugin.hermes_python(home)
    if not python.exists():
        return _check("playwright", "fail", f"Hermes Python was not found: {python}")
    code, output = _run([str(python), "-c", "import playwright.sync_api; print('playwright ok')"], timeout=20)
    if code != 0:
        return _check("playwright", "fail", "Python Playwright is not installed in the Hermes environment", python=str(python), output=output[-500:])
    code, chromium_output = _run([str(python), "-m", "playwright", "install", "chromium", "--dry-run"], timeout=30)
    status = "ok" if code == 0 else "warn"
    message = "Playwright package is installed" if status == "ok" else "Playwright package is installed, but Chromium dry-run check failed"
    return _check("playwright", status, message, python=str(python), output=chromium_output[-500:])


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
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return _check("cassette_url", "warn", "Cassette URL is not HTTP(S); connectivity check skipped", url=url)
    last_error = ""
    for method in ("HEAD", "GET"):
        try:
            request = Request(url, method=method, headers={"User-Agent": "oh-my-cassette-diagnose/1.0"})
            with urlopen(request, timeout=10) as response:
                status = int(getattr(response, "status", 200) or 200)
            if 200 <= status < 400 or status in {401, 403}:
                return _check("cassette_url", "ok", "Cassette URL is reachable", url=url, http_status=status)
            return _check("cassette_url", "fail", "Cassette URL returned an unhealthy status", url=url, http_status=status)
        except HTTPError as exc:
            if int(exc.code) in {401, 403}:
                return _check("cassette_url", "ok", "Cassette URL is reachable and requires auth", url=url, http_status=int(exc.code))
            if method == "HEAD" and int(exc.code) in {405, 501}:
                last_error = f"HTTP {exc.code}"
                continue
            return _check("cassette_url", "fail", "Cassette URL request failed", url=url, http_status=int(exc.code))
        except (TimeoutError, URLError, OSError) as exc:
            last_error = type(exc).__name__
            if method == "HEAD":
                continue
            return _check("cassette_url", "fail", "Cassette URL is not reachable", url=url, error=last_error)
    return _check("cassette_url", "fail", "Cassette URL is not reachable", url=url, error=last_error)


CASSETTE_LOGIN_CHECK_SCRIPT = r'''
from __future__ import annotations

import json
import os
import time

from playwright.sync_api import sync_playwright


def print_result(status, **payload):
    payload["status"] = status
    print(json.dumps(payload, ensure_ascii=False))


def visible_selectors(page, selectors):
    try:
        return page.evaluate(
            """(selectors) => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                return selectors.filter((selector) => {
                    try {
                        return Array.from(document.querySelectorAll(selector)).some(visible);
                    } catch (_) {
                        return false;
                    }
                });
            }""",
            selectors,
        )
    except Exception:
        return []


def auth_element_state(page):
    try:
        return page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const signupEmail = document.querySelector("#agent-auth-email");
                const loginEmail = document.querySelector("#agent-auth-email-login");
                const password = document.querySelector("#agent-auth-password");
                return {
                    signup_email_visible: visible(signupEmail),
                    login_email_visible: visible(loginEmail),
                    password_visible: visible(password),
                };
            }"""
        )
    except Exception:
        return {}


def page_requires_auth(page):
    state = auth_element_state(page)
    return bool(
        state.get("signup_email_visible")
        or (state.get("login_email_visible") and state.get("password_visible"))
    )


def switch_to_login_form(page):
    state = auth_element_state(page)
    if state.get("login_email_visible") and state.get("password_visible"):
        return
    if not state.get("signup_email_visible"):
        return
    try:
        page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const signupEmail = document.querySelector("#agent-auth-email");
                const form = signupEmail?.closest("form");
                const root = form?.parentElement || signupEmail?.closest("main,section") || document.body;
                const formRect = form?.getBoundingClientRect();
                const buttons = Array.from(root.querySelectorAll("button[type='button'],button:not([type])"))
                    .filter((button) => {
                        if (!visible(button) || button.disabled || button.getAttribute("aria-disabled") === "true") return false;
                        if (button.closest("form")) return false;
                        if (formRect) {
                            const rect = button.getBoundingClientRect();
                            if (rect.bottom > formRect.top + 2) return false;
                        }
                        return true;
                    });
                const labels = (button) => [
                    button.getAttribute("aria-label"),
                    button.getAttribute("title"),
                    button.getAttribute("data-value"),
                    button.getAttribute("value"),
                    button.id,
                    button.name,
                    button.innerText,
                    button.textContent,
                ].filter(Boolean).join(" ").replace(/\\s+/g, " ").trim().toLowerCase();
                const target = buttons.find((button) => {
                    const label = labels(button);
                    return /(^|\\b)(log in|login|sign in|signin)(\\b|$)/.test(label) || /登录|登入|登陆/.test(label);
                }) || (() => {
                    const wideButtons = buttons.filter((button) => {
                        const rect = button.getBoundingClientRect();
                        return rect.width * rect.height >= 1200;
                    });
                    const tabButtons = wideButtons.length >= 2 ? wideButtons : buttons;
                    return tabButtons.length >= 2 ? tabButtons[1] : null;
                })();
                if (target) target.click();
            }"""
        )
    except Exception:
        pass


def agent_ui_ready(page):
    matches = visible_selectors(
        page,
        [
            "[data-testid='agent-upload-status']",
            "[data-testid='agent-export-button']",
            "[data-testid='agent-chat-input']",
            "[data-testid='chat-input']",
            "textarea[placeholder*='Describe']",
            "textarea[placeholder*='描述']",
            "textarea",
            "[role='textbox']",
            "[contenteditable='true']",
        ],
    )
    return bool(matches) and not page_requires_auth(page)


def wait_state(page, timeout_sec, auth_immediate=True):
    deadline = time.monotonic() + timeout_sec
    saw_auth = False
    while time.monotonic() < deadline:
        auth_visible = page_requires_auth(page)
        if auth_visible:
            saw_auth = True
        if auth_visible and auth_immediate:
            return "auth"
        if agent_ui_ready(page):
            return "ready"
        time.sleep(0.25)
    return "auth" if saw_auth else "unknown"


def first_visible_locator(page, selectors, timeout_sec=5):
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        for selector in selectors:
            try:
                locators = page.locator(selector)
                count = min(locators.count(), 20)
            except Exception:
                continue
            for index in range(count):
                locator = locators.nth(index)
                try:
                    if locator.is_visible():
                        return locator
                except Exception:
                    continue
        time.sleep(0.1)
    return None


def main():
    url = os.environ["CASSETTE_DIAG_URL"]
    email = os.environ["CASSETTE_DIAG_EMAIL"]
    password = os.environ["CASSETTE_DIAG_PASSWORD"]
    no_sandbox = os.environ.get("CASSETTE_NO_SANDBOX", "").lower() in {"1", "true", "yes", "on"}
    launch_args = ["--no-sandbox"] if no_sandbox else []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=launch_args)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            state = wait_state(page, 20)
            if state == "ready":
                print_result("ok", code="already_authenticated_or_no_auth")
                return
            if state != "auth":
                print_result("fail", code="cassette_ui_not_ready")
                return

            switch_to_login_form(page)
            login_deadline = time.monotonic() + 5
            while time.monotonic() < login_deadline:
                state = auth_element_state(page)
                if state.get("login_email_visible") and state.get("password_visible"):
                    break
                time.sleep(0.1)
            email_input = first_visible_locator(
                page,
                [
                    "#agent-auth-email-login",
                    "input[type='email'][autocomplete='email']",
                    "input[type='email']",
                ],
                timeout_sec=5,
            )
            password_input = first_visible_locator(page, ["#agent-auth-password", "input[type='password']"], timeout_sec=5)
            if not email_input or not password_input:
                print_result("fail", code="cassette_auth_form_missing", auth_state=auth_element_state(page))
                return
            email_input.fill(email)
            password_input.fill(password)
            password_input.press("Enter")
            post_auth_state = wait_state(page, 45, auth_immediate=False)
            if post_auth_state == "ready":
                print_result("ok", code="authenticated")
                return
            if page_requires_auth(page):
                visible_auth = visible_selectors(
                    page,
                    [
                        "#agent-auth-password",
                        "#agent-auth-email-login",
                        "#agent-auth-email",
                        "input[type='password']",
                    ],
                )
                if visible_auth:
                    print_result("fail", code="cassette_auth_form_still_visible", auth_selectors=visible_auth)
                    return
                print_result("fail", code="cassette_post_auth_ui_not_ready")
                return
            print_result("fail", code="cassette_post_auth_ui_not_ready")
        finally:
            browser.close()


try:
    main()
except Exception as exc:
    print_result("fail", code=type(exc).__name__)
'''


def _check_cassette_login(home: Path, url: str, email: str, password: str) -> dict:
    if not email or not password:
        return _check("cassette_login", "warn", "Cassette login credentials are not configured; login verification skipped")
    python = install_plugin.hermes_python(home)
    if not python.exists():
        return _check("cassette_login", "fail", f"Hermes Python was not found: {python}")
    env = os.environ.copy()
    env.update(
        {
            "CASSETTE_DIAG_URL": url,
            "CASSETTE_DIAG_EMAIL": email,
            "CASSETTE_DIAG_PASSWORD": password,
        }
    )
    try:
        proc = subprocess.run(
            [str(python), "-c", CASSETTE_LOGIN_CHECK_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=90,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return _check("cassette_login", "fail", "Cassette login verification timed out")
    except Exception as exc:
        return _check("cassette_login", "fail", f"Cassette login verification failed to run: {type(exc).__name__}")

    output = _sanitize_text((proc.stdout or "").strip())
    data: dict = {}
    if output:
        try:
            data = json.loads(output.splitlines()[-1])
        except json.JSONDecodeError:
            data = {}
    if proc.returncode != 0 and not data:
        return _check("cassette_login", "fail", "Cassette login verification process failed", output=output[-1000:])
    code = str(data.get("code") or "unknown")
    if data.get("status") == "ok":
        return _check("cassette_login", "ok", "Cassette login credentials were accepted", code=code)
    if code == "cassette_ui_not_ready":
        return _check("cassette_login", "warn", "Cassette page loaded but login/agent UI was not ready during verification", code=code)
    if code in {"cassette_auth_form_missing", "cassette_auth_form_still_visible", "cassette_post_auth_ui_not_ready"}:
        return _check("cassette_login", "warn", "Cassette credentials were not rejected, but the diagnostic browser did not reach the agent UI", code=code, output=output[-1000:])
    message = "Cassette login credentials were rejected or login did not complete"
    return _check("cassette_login", "fail", message, code=code, output=output[-1000:])


def diagnose(home: Path, repo: Path) -> list[dict]:
    env_values = install_plugin.read_env_values(home / ".env")
    url = env_values.get("CASSETTE_URL") or install_plugin.CASSETTE_DEFAULT_URL
    return [
        _check_plugin(home, repo),
        _check_plugin_enabled(home),
        _check_env(home),
        _check_binary("ffmpeg", env_values.get("CASSETTE_FFMPEG_BIN", "")),
        _check_binary("ffprobe", env_values.get("CASSETTE_FFPROBE_BIN", "")),
        _check_playwright(home),
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
