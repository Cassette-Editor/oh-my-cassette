from __future__ import annotations

import argparse
import importlib.util
import sys
import traceback
from pathlib import Path

if __package__:
    from . import browser, jobs, notifier, transport
else:
    root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("cassette", root / "__init__.py", submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["cassette"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    from cassette import browser, jobs, notifier, transport


def run(job_id: str, action: str = "run") -> dict:
    job = jobs.update_job(job_id, status="running", started_at=jobs.now_iso(), finished_at=None)
    try:
        if jobs.is_cancel_requested(job_id):
            job = jobs.update_job(job_id, status="cancelled", finished_at=jobs.now_iso())
            job["notification"] = notifier.notify_terminal_job(job)
            jobs.save_job(job)
            return job
        # Detached subprocess: keep the browser path on the original non-threaded entrypoint
        # (byte-identical); route only the API transport through the seam.
        if action == "resume":
            request = job.get("resume_request") if isinstance(job.get("resume_request"), dict) else {}
            response = str(request.get("response") or "")
            result = transport.get_transport().resume(job, response)
        elif transport.selected_transport() == transport.TRANSPORT_API:
            result = transport.get_transport().run_job(job)
        else:
            result = browser.run_cassette_browser_job(job)
        job = jobs.merge_persisted_runtime_fields(job)
        job.update(result)
        job["status"] = result.get("status", "failed")
        job["finished_at"] = jobs.now_iso()
        job.pop("resume_request", None)
        if job["status"] != "needs_user":
            job.pop("continuation", None)
        jobs.save_job(job)
        job["notification"] = notifier.notify_terminal_job(job)
        jobs.save_job(job)
        return job
    except Exception as exc:
        job = jobs.update_job(
            job_id,
            status="failed",
            errors=[{"code": "internal_error", "message": str(exc), "details": {"type": type(exc).__name__, "trace": traceback.format_exc(limit=3)}}],
            finished_at=jobs.now_iso(),
        )
        job["notification"] = notifier.notify_terminal_job(job)
        jobs.save_job(job)
        return job


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--action", choices=("run", "resume"), default="run")
    args = parser.parse_args()
    run(args.job_id, args.action)


if __name__ == "__main__":
    main()
