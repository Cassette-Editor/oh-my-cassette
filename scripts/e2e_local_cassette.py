#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from e2e_common import json_stdout, load_cassette_package, output_links, safe_error_codes


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _tool_payload(result: str) -> dict:
    try:
        data = json.loads(result)
        return data if isinstance(data, dict) else {"ok": False, "error": {"code": "invalid_tool_result"}}
    except json.JSONDecodeError:
        return {"ok": False, "error": {"code": "invalid_json_tool_result"}}


def _failure(code: str, message: str, job: dict | None = None, result_path: str = "") -> int:
    json_stdout(
        {
            "success": False,
            "transport": os.getenv("CASSETTE_TRANSPORT", "browser"),
            "job_id": str((job or {}).get("job_id") or ""),
            "status": str((job or {}).get("status") or code),
            "manifest_path": "",
            "result_path": result_path,
            "output_links": output_links(job or {}),
            "errors": [{"code": code, "message": message}],
        }
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local Cassette E2E without WeChat.")
    parser.add_argument("--media", default="tests/fixtures/sample.mp4")
    parser.add_argument("--instruction", default="帮我剪成 10 秒以内的短视频，加中文字幕")
    parser.add_argument("--wait", default="true", help="true or false; false validates detached worker")
    parser.add_argument("--timeout-sec", type=int, default=int(os.getenv("CASSETTE_E2E_TIMEOUT_SEC", "1200")))
    parser.add_argument("--session-id", default=f"local-e2e-{int(time.time())}")
    parser.add_argument(
        "--transport",
        choices=["env", "browser", "api"],
        default="env",
        help="Force the Cassette transport (browser|api) or 'env' to honor CASSETTE_TRANSPORT.",
    )
    args = parser.parse_args()

    # Select the transport before the package loads so the seam picks it up deterministically.
    if args.transport != "env":
        os.environ["CASSETTE_TRANSPORT"] = args.transport

    media = Path(args.media).expanduser().resolve()
    if not media.exists():
        return _failure("media_not_found", f"Media fixture not found: {args.media}")

    roots = [item for item in os.getenv("CASSETTE_ALLOWED_SOURCE_ROOTS", "").split(os.pathsep) if item]
    media_parent = str(media.parent)
    if media_parent not in roots:
        roots.append(media_parent)
    os.environ["CASSETTE_ALLOWED_SOURCE_ROOTS"] = os.pathsep.join(roots)
    os.environ.setdefault(
        "CASSETTE_ALLOWED_EXTENSIONS", ".mp4,.mov,.m4v,.webm,.jpg,.jpeg,.png,.webp,.gif,.mp3,.wav,.m4a,.aac"
    )
    os.environ.setdefault("CASSETTE_MAX_BYTES", "2147483648")
    load_cassette_package()

    from cassette import manifest as cassette_manifest
    from cassette import tools

    ingest = _tool_payload(
        tools.cassette_ingest_media(
            {
                "source_path": str(media),
                "original_name": media.name,
                "media_type": "video",
                "session_id": args.session_id,
                "caption": "local e2e fixture",
            }
        )
    )
    if not ingest.get("ok"):
        error = ingest.get("error") or {}
        return _failure(str(error.get("code") or "ingest_failed"), "cassette_ingest_media failed")

    made = _tool_payload(
        tools.cassette_make_prompt(
            {
                "instruction": args.instruction,
                "session_id": args.session_id,
                "requires_assets": True,
            }
        )
    )
    if not made.get("ok"):
        error = made.get("error") or {}
        return _failure(str(error.get("code") or "make_prompt_failed"), "cassette_make_prompt failed")

    wait = _parse_bool(str(args.wait))
    run = _tool_payload(
        tools.cassette_run_job(
            {
                "prompt": made["data"]["prompt"],
                "instruction": args.instruction,
                "session_id": args.session_id,
                "wait": wait,
                "timeout_sec": args.timeout_sec,
            }
        )
    )
    if not run.get("ok"):
        error = run.get("error") or {}
        return _failure(str(error.get("code") or "run_job_failed"), "cassette_run_job failed")

    job = run["data"]["job"]
    if not wait:
        deadline = time.monotonic() + args.timeout_sec
        while time.monotonic() < deadline:
            status_payload = _tool_payload(tools.cassette_job_status({"job_id": job["job_id"]}))
            if status_payload.get("ok"):
                job = status_payload["data"]["job"]
                if job.get("status") in {"succeeded", "failed", "needs_user", "timed_out", "timeout", "cancelled"}:
                    break
            time.sleep(2)
        else:
            job["status"] = "timeout"

    status = str(job.get("status") or "unknown")
    links = output_links(job)
    errors = safe_error_codes(job.get("errors"))
    success = status == os.getenv("CASSETTE_E2E_EXPECT_STATUS", "succeeded")
    if os.getenv("CASSETTE_E2E_EXPECT_OUTPUT_LINK", "true").lower() in {"1", "true", "yes"} and not links:
        success = False
        errors.append({"code": "missing_output_link"})
    if status == "needs_user":
        success = False
        errors.append({"code": "needs_user", "message": "Cassette requires manual confirmation or additional media"})
    elif status in {"failed", "timed_out", "timeout", "cancelled"} and not errors:
        errors.append({"code": status})

    json_stdout(
        {
            "success": success,
            "transport": os.getenv("CASSETTE_TRANSPORT", "browser"),
            "job_id": str(job.get("job_id") or ""),
            "status": status,
            "manifest_path": made["data"].get("manifest_path", ""),
            "result_path": str(cassette_manifest.get_asset_root() / "jobs" / f"{job.get('job_id')}.json")
            if job.get("job_id")
            else "",
            "output_links": links,
            "errors": errors,
        }
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
