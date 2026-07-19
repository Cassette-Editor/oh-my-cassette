#!/usr/bin/env python3
"""Browser-vs-API transport parity harness.

Runs the SAME ingest -> make_prompt -> run_job -> status flow through both transports
(``CASSETTE_TRANSPORT=browser`` then ``=api``) via e2e_local_cassette.py, then diffs the
structured outcome. Parity holds when both runs agree on:

  * terminal status (succeeded / failed / needs_user / ...)
  * number of deliverable outputs (output_links)
  * the set of error codes

This is the verification the user asked for: the existing unit suite proves the seam +
downstream contract offline; this proves the API transport produces the SAME observable
outcome as the trusted browser transport against a live Cassette.

Requirements for the API leg: CASSETTE_API_URL set to the render-server origin, a
CASSETTE_AUTH_EMAIL with 'full' allowlist access, the /api/export/projects/:id/jobs endpoint
deployed, RENDER_PROVIDER=lambda, and (for the browser leg) Playwright installed. Gate live
runs with RUN_CASSETTE_E2E=1 as usual.

Usage:
  python scripts/e2e_transport_parity.py --media tests/fixtures/sample.mp4 \
      --instruction "Make a short captioned video under 10 seconds."
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DRIVER = Path(__file__).resolve().parent / "e2e_local_cassette.py"


def _run(transport: str, args: argparse.Namespace) -> dict:
    env = dict(os.environ)
    env["CASSETTE_TRANSPORT"] = transport
    cmd = [
        sys.executable,
        str(DRIVER),
        "--transport",
        transport,
        "--media",
        args.media,
        "--instruction",
        args.instruction,
        "--wait",
        args.wait,
        "--timeout-sec",
        str(args.timeout_sec),
        "--session-id",
        f"{args.session_prefix}-{transport}-{int(time.time())}",
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    payload = _last_json_line(proc.stdout)
    if payload is None:
        payload = {
            "success": False,
            "transport": transport,
            "status": "harness_error",
            "output_links": [],
            "errors": [{"code": "no_json_output"}],
            "_stderr_tail": proc.stderr[-2000:],
        }
    payload["_exit_code"] = proc.returncode
    return payload


def _last_json_line(stdout: str) -> dict | None:
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _error_codes(payload: dict) -> list[str]:
    return sorted({str(e.get("code")) for e in (payload.get("errors") or []) if isinstance(e, dict)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Diff browser vs API Cassette transport outcomes.")
    parser.add_argument("--media", default="tests/fixtures/sample.mp4")
    parser.add_argument("--instruction", default="Make a short captioned video under 10 seconds.")
    parser.add_argument("--wait", default="true")
    parser.add_argument("--timeout-sec", type=int, default=int(os.getenv("CASSETTE_E2E_TIMEOUT_SEC", "1200")))
    parser.add_argument("--session-prefix", default="parity")
    args = parser.parse_args()

    browser = _run("browser", args)
    api = _run("api", args)

    status_match = browser.get("status") == api.get("status")
    links_match = len(browser.get("output_links") or []) == len(api.get("output_links") or [])
    errors_match = _error_codes(browser) == _error_codes(api)
    parity = status_match and links_match and errors_match

    print(
        json.dumps(
            {
                "parity": parity,
                "checks": {"status": status_match, "output_link_count": links_match, "error_codes": errors_match},
                "browser": {
                    "status": browser.get("status"),
                    "output_links": len(browser.get("output_links") or []),
                    "error_codes": _error_codes(browser),
                },
                "api": {
                    "status": api.get("status"),
                    "output_links": len(api.get("output_links") or []),
                    "error_codes": _error_codes(api),
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0 if parity else 1


if __name__ == "__main__":
    sys.exit(main())
