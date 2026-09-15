# Backend contract samples

Real responses captured from a local Cassette-Editor `main` stack (commit 5e8c4f9df, 2026-09-15) with
`AGENT_AUTH_ENABLED=false`. `tests/test_models.py` validates every file against the pydantic mirrors in
`oh_my_cassette.cassette.models`, so a drift in either direction turns the suite red.

| file | route |
| --- | --- |
| `project-snapshot.json` | `POST /api/projects/:id/initialize` (same envelope as `GET /api/projects/:id`) |
| `chat-session.json` | `POST /api/projects/:id/chat-sessions` |
| `session-state.json` | `GET /api/agent/sessions/:cs/state?projectId=` |
| `command-start-result.json` | `POST /api/agent/sessions/:cs/commands` (`start`) |
| `run-events.jsonl` | `GET /api/agent/sessions/:cs/events` durable + transient frames, one JSON per line |
| `media-operations-status.json` | `GET /api/media/operations/status?ids=` (urls redacted, metadata trimmed) |
| `media-files.json` | `GET /api/media/files?session_id=` (urls redacted, metadata trimmed) |
| `import-manifest.json` | `POST /api/media/imports` |
| `timeline-history.json` | `GET /api/projects/:id/timeline-history` |

Refresh them with the live stack running: see `tests/live/test_live_stack.py`.
