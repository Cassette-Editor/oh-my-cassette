#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

from e2e_common import (
    env_bool,
    find_session_manifest,
    json_stdout,
    output_links,
    safe_error_codes,
    wait_for_latest_job,
)


def _search_roots(job_root: Path, media_dir: Path) -> list[Path]:
    roots = [
        media_dir,
        job_root,
        media_dir / "jobs",
        job_root / "jobs",
        media_dir.parent / "jobs",
        job_root.parent / "jobs",
    ]
    asset_root = os.getenv("CASSETTE_ASSET_ROOT")
    if asset_root:
        asset = Path(asset_root).expanduser().resolve()
        roots.extend([asset, asset / "jobs"])
    seen: set[str] = set()
    deduped: list[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def main() -> int:
    job_root_raw = os.getenv("CASSETTE_E2E_JOB_ROOT")
    media_dir_raw = os.getenv("CASSETTE_MEDIA_DIR") or job_root_raw
    if not job_root_raw:
        json_stdout(
            {
                "success": False,
                "job_id": "",
                "status": "configuration_error",
                "manifest_path": "",
                "result_path": "",
                "output_links": [],
                "errors": [{"code": "missing_env", "message": "CASSETTE_E2E_JOB_ROOT is required"}],
            }
        )
        return 2

    job_root = Path(job_root_raw).expanduser().resolve()
    media_dir = Path(media_dir_raw).expanduser().resolve() if media_dir_raw else job_root
    timeout_sec = _int_env("CASSETTE_E2E_TIMEOUT_SEC", 1200)
    expected_status = os.getenv("CASSETTE_E2E_EXPECT_STATUS", "succeeded")
    expect_output_link = env_bool("CASSETTE_E2E_EXPECT_OUTPUT_LINK", True)
    search_roots = _search_roots(job_root, media_dir)

    job_path, job, timed_out = wait_for_latest_job(search_roots, timeout_sec)
    if timed_out:
        json_stdout(
            {
                "success": False,
                "job_id": str((job or {}).get("job_id") or ""),
                "status": "timeout",
                "manifest_path": find_session_manifest(job or {}, *search_roots) if job else "",
                "result_path": str(job_path or ""),
                "output_links": output_links(job or {}),
                "errors": [{"code": "timeout", "message": f"No terminal Cassette job status within {timeout_sec}s"}],
            }
        )
        return 1

    job = job or {}
    status = str(job.get("status") or "unknown")
    links = output_links(job)
    errors = safe_error_codes(job.get("errors"))
    exit_code = 0
    success = status == expected_status

    if status == "needs_user":
        success = False
        errors.append({"code": "needs_user", "message": "Cassette requires manual confirmation or additional media"})
        exit_code = 1
    elif status in {"failed", "timed_out", "timeout", "cancelled"}:
        success = False
        if not errors:
            errors.append({"code": status})
        exit_code = 1
    elif status != expected_status:
        success = False
        errors.append({"code": "unexpected_status", "message": f"Expected {expected_status}, got {status}"})
        exit_code = 1

    if expect_output_link and not links:
        success = False
        errors.append({"code": "missing_output_link", "message": "No output link was recorded"})
        exit_code = 1

    json_stdout(
        {
            "success": success,
            "job_id": str(job.get("job_id") or ""),
            "status": status,
            "manifest_path": find_session_manifest(job, *search_roots),
            "result_path": str(job_path or ""),
            "output_links": links,
            "errors": errors,
        }
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
