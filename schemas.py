from __future__ import annotations

CASSETTE_INGEST_MEDIA = {
    "name": "cassette_ingest_media",
    "description": "Safely ingest a media file already downloaded by a Hermes gateway adapter from an allowed local cache root, copy it into the Cassette asset root, and update the session manifest.",
    "parameters": {
        "type": "object",
        "properties": {
            "source_path": {
                "type": "string",
                "description": "Local gateway cache path. Do not use arbitrary user-supplied paths.",
            },
            "original_name": {"type": "string"},
            "media_type": {"type": "string", "enum": ["video", "image", "audio", "file", "unknown"]},
            "chat_id": {"type": "string"},
            "user_id": {"type": "string"},
            "message_id": {"type": "string"},
            "chat_type": {"type": "string"},
            "thread_id": {"type": "string"},
            "platform": {"type": "string"},
            "caption": {"type": "string"},
            "session_id": {"type": "string"},
        },
        "required": ["source_path"],
        "additionalProperties": False,
    },
}

CASSETTE_LIST_ASSETS = {
    "name": "cassette_list_assets",
    "description": "Read the current Cassette session manifest and update asset existence flags.",
    "parameters": {
        "type": "object",
        "properties": {"session_id": {"type": "string"}, "chat_id": {"type": "string"}},
        "additionalProperties": False,
    },
}

CASSETTE_MAKE_PROMPT = {
    "name": "cassette_make_prompt",
    "description": "Turn a natural-language video editing instruction and session manifest into a complete non-blocking Cassette prompt.",
    "parameters": {
        "type": "object",
        "properties": {
            "instruction": {"type": "string"},
            "session_id": {"type": "string"},
            "chat_id": {"type": "string"},
            "requires_assets": {"type": "boolean", "default": True},
            "output_format": {"type": "string"},
            "duration": {"type": "string"},
            "style": {"type": "string"},
            "cassette_language": {
                "type": "string",
                "enum": ["zh", "en"],
                "description": "Cassette UI/chat language. QQ defaults to zh; Telegram defaults to en.",
            },
            "language": {"type": "string", "enum": ["zh", "en"], "description": "Alias for cassette_language."},
            "constraints": {"type": "object"},
        },
        "required": ["instruction"],
        "additionalProperties": False,
    },
}

CASSETTE_ANSWER_QUESTION = {
    "name": "cassette_answer_question",
    "description": "Classify a Cassette follow-up question, or resume a user-input-paused job with job_id and response.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "instruction": {"type": "string"},
            "asset_count": {"type": "integer"},
            "context": {"type": "object"},
            "job_id": {"type": "string", "description": "Resume mode: persisted job waiting for user input."},
            "response": {
                "type": "string",
                "description": "Resume mode: validated user response for the pending question.",
            },
        },
        "anyOf": [
            {"required": ["question"]},
            {"required": ["job_id", "response"]},
        ],
        "additionalProperties": False,
    },
}

CASSETTE_MATCH_BGM = {
    "name": "cassette_match_bgm",
    "description": "Search Free To Use for a smart BGM track using Hermes-selected category/tag search queries, download one matched MP3, and register it as an audio asset in the active Cassette session.",
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "instruction": {
                "type": "string",
                "description": "The user's original or plugin-augmented editing instruction.",
            },
            "search_queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One to three English search queries based on Free To Use category names plus related tag/mood words.",
            },
            "optimization_enabled": {
                "type": "boolean",
                "description": "Whether the user chose prompt optimization before smart BGM selection.",
            },
            "continue_after_match": {
                "type": "boolean",
                "description": "Default true. Set false for /music standalone matching so the tool only registers a BGM asset and does not guide Hermes into Cassette execution.",
            },
            "fallback_from": {
                "type": "string",
                "description": "Optional. Set to exact_bgm when this Free To Use match is a fallback after exact song matching failed.",
            },
            "fallback_reason": {
                "type": "string",
                "description": "Optional concise error code or reason from the primary BGM provider that triggered fallback.",
            },
        },
        "required": ["session_id", "instruction", "search_queries"],
        "additionalProperties": False,
    },
}

CASSETTE_MATCH_EXACT_BGM = {
    "name": "cassette_match_exact_bgm",
    "description": (
        "Exact song/artist smart BGM matcher. Hermes provides a concrete song title and artist chosen from user-facing recommendations; "
        "the plugin searches MusicSquare-style aggregated sources, first by title+artist and then by title only if needed, downloads the deterministic eligible match, and registers it as a Cassette audio asset."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "instruction": {
                "type": "string",
                "description": "The user's original or plugin-augmented editing instruction.",
            },
            "title": {
                "type": "string",
                "description": "Concrete song title selected by the user or by Hermes for random-provider mode.",
            },
            "songTitle": {"type": "string", "description": "Alias for title."},
            "song_title": {"type": "string", "description": "Alias for title."},
            "artist": {
                "type": "string",
                "description": "Concrete artist/singer name. Strongly preferred for exact search.",
            },
            "singer": {"type": "string", "description": "Alias for artist."},
            "optimization_enabled": {
                "type": "boolean",
                "description": "Whether the user chose prompt optimization before smart BGM selection.",
            },
            "continue_after_match": {
                "type": "boolean",
                "description": "Default true. Set false for /music standalone matching so the tool only registers a BGM asset and does not guide Hermes into Cassette execution.",
            },
            "download": {
                "type": "boolean",
                "default": True,
                "description": "If false, only searches and returns eligible candidates.",
            },
        },
        "required": ["session_id", "instruction", "title"],
        "additionalProperties": False,
    },
}

JAMENDO_MUSIC_MATCHER = {
    "name": "jamendo_music_matcher",
    "description": (
        "Fixed-form Jamendo music matcher. Hermes fills controlled fields such as searchTerms/fuzzyTags/vocalInstrumental; the plugin builds safe Jamendo strategies internally. "
        "The plugin searches multiple result orders/boosts, retries zero-result searches up to a 3-attempt Jamendo budget, and only then lets Hermes fall back to Free To Use. Never ask Hermes to generate or print raw Jamendo SearchPlan JSON."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "userQuery": {"type": "string", "description": "Natural-language music requirement from the user."},
            "user_query": {"type": "string", "description": "Alias for userQuery."},
            "searchTerms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Required fixed-form mode. One to five short English Jamendo-friendly search phrases, for example gentle male vocal pop.",
            },
            "search_terms": {"type": "array", "items": {"type": "string"}, "description": "Alias for searchTerms."},
            "fuzzyTags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional English mood/genre/instrument words used as Jamendo fuzzytags.",
            },
            "fuzzy_tags": {"type": "array", "items": {"type": "string"}, "description": "Alias for fuzzyTags."},
            "excludeTerms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional English terms to exclude locally from candidate title/artist/album.",
            },
            "exclude_terms": {"type": "array", "items": {"type": "string"}, "description": "Alias for excludeTerms."},
            "vocalInstrumental": {
                "type": "string",
                "enum": ["vocal", "instrumental"],
                "description": "Optional vocal preference when the user clearly requested vocal or instrumental.",
            },
            "vocalinstrumental": {
                "type": "string",
                "enum": ["vocal", "instrumental"],
                "description": "Alias for vocalInstrumental.",
            },
            "searchPlan": {
                "type": ["object", "string"],
                "description": "Legacy compatibility only. Do not use for new tool calls.",
            },
            "search_plan": {
                "type": ["object", "string"],
                "description": "Legacy compatibility only. Do not use for new tool calls.",
            },
            "repairJson": {
                "type": ["object", "string"],
                "description": "Optional repaired SearchPlan JSON if the first Hermes JSON was invalid.",
            },
            "download": {
                "type": "boolean",
                "default": True,
                "description": "If false, only returns eligible candidates and does not download.",
            },
            "seed": {"type": "integer", "description": "Optional uniform-random seed for reproducible selection."},
            "limit": {"type": "integer", "description": "Optional per-search-term limit, capped at 50."},
            "limitOverride": {"type": "integer", "description": "Optional per-strategy limit override, capped at 200."},
            "outputDir": {
                "type": "string",
                "description": "Optional download directory. Relative paths are under CASSETTE_ASSET_ROOT; absolute paths must also be under CASSETTE_ASSET_ROOT.",
            },
            "session_id": {
                "type": "string",
                "description": "Optional Cassette session id; when provided, downloaded MP3 is registered as an audio asset.",
            },
        },
        "required": ["userQuery", "searchTerms"],
        "additionalProperties": False,
    },
}

CASSETTE_RUN_JOB = {
    "name": "cassette_run_job",
    "description": "Run one conversational turn with the Cassette agent: upload any new session assets, send the user's verbatim message to the session's persistent agent thread, monitor completion, and persist job status. The same session keeps one thread, so follow-up turns share memory. Turns end without rendering; pass export=true only when the user expresses finish/export intent. For QQ, Telegram, and Weixin gateway jobs with a stored delivery target, the plugin runs the job in the background and Hermes should stop the turn after reporting that the job started; do not repeatedly poll cassette_job_status unless the user explicitly asks.",
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The user's verbatim words for this conversational turn. Preferred input; passed to the Cassette agent unmodified — never rewrite, optimize, or expand them.",
            },
            "export": {
                "type": "boolean",
                "default": False,
                "description": "Render/export at the end of this turn. Pass true only when the user expresses finish/export intent; the turn then ends in completion review gating the render.",
            },
            "prompt": {
                "type": "string",
                "description": "Legacy browser-transport brief (cassette_make_prompt.data.prompt). On the API transport prefer message.",
            },
            "chat_message": {
                "type": "string",
                "description": "User-facing edit request to send into Cassette chat panel. Pass cassette_make_prompt.data.chat_message. Do not pass Hermes internal planning prompts here.",
            },
            "cassette_message": {"type": "string", "description": "Alias for chat_message."},
            "instruction": {"type": "string"},
            "session_id": {"type": "string"},
            "chat_id": {"type": "string"},
            "url": {"type": "string"},
            "wait": {
                "type": "boolean",
                "default": True,
                "description": "For gateway jobs pass false to keep slash commands responsive. Background gateway jobs notify progress/final status themselves; avoid automatic cassette_job_status polling.",
            },
            "timeout_sec": {"type": "integer"},
            "selectors": {"type": "object"},
            "cassette_model": {
                "type": "string",
                "description": "Optional Cassette model label, for example DeepSeek V4 Flash. Defaults to DeepSeek V4 Flash.",
            },
            "model": {"type": "string", "description": "Alias for cassette_model."},
            "thinking_level": {
                "type": "string",
                "description": "Optional Cassette thinking level: low, medium, high, or Chinese equivalents. Defaults to low.",
            },
            "cassette_language": {
                "type": "string",
                "enum": ["zh", "en"],
                "description": "Cassette UI/chat language. QQ defaults to zh; Telegram defaults to en.",
            },
            "language": {"type": "string", "enum": ["zh", "en"], "description": "Alias for cassette_language."},
        },
        "anyOf": [{"required": ["message"]}, {"required": ["prompt"]}],
        "additionalProperties": False,
    },
}

CASSETTE_JOB_STATUS = {
    "name": "cassette_job_status",
    "description": "Return one Cassette job by job_id or recent jobs for a session. Use for explicit status requests or non-gateway checks; do not tight-poll running gateway background jobs.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "session_id": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "additionalProperties": False,
    },
}

CASSETTE_REVIEW_COMPLETION = {
    "name": "cassette_review_completion",
    "description": (
        "Hermes supervisor decision for a Cassette job that reached export-enabled state without an unambiguous completion signal. "
        "Use decision=export only when Hermes judges the latest Cassette assistant reply means the requested edit is complete enough to export."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "decision": {"type": "string", "enum": ["export", "continue", "needs_user", "failed"]},
            "reason": {
                "type": "string",
                "description": "Short Hermes supervisor rationale. Do not include local paths or raw IDs.",
            },
            "summary": {"type": "string", "description": "Optional user-safe summary of the Cassette reply."},
        },
        "required": ["job_id", "decision", "reason"],
        "additionalProperties": False,
    },
}

CASSETTE_TIMELINE = {
    "name": "cassette_timeline",
    "description": (
        "Read the live Cassette project timeline as a bounded text digest (CTL). Use it before any "
        "statement about project state — never answer from memory. Optional contact_sheet tiles the "
        "stored clip posters into one image (zero render)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "detail": {
                "type": "string",
                "description": "Expand one track fully, e.g. 'track:V1'. Omit for the capped digest.",
            },
            "profile": {
                "type": "string",
                "enum": ["aligned", "gateway"],
                "description": "aligned = fixed-width for monospace surfaces; gateway = unpadded for chat apps.",
            },
            "contact_sheet": {"type": "boolean", "default": False},
        },
        "required": ["session_id"],
        "additionalProperties": False,
    },
}

CASSETTE_EDIT = {
    "name": "cassette_edit",
    "description": (
        "Surgical no-LLM timeline edit through the manual-editor command lane (requires "
        "CASSETTE_DIRECT_EDIT=1). Use for small named changes (trim, retime, text, delete, undo) "
        "after reading cassette_timeline; big or creative briefs go through cassette_run_job. "
        "Pass expected_version from the last timeline read — a stale version returns "
        "stale_timeline with a fresh digest. tool_name 'undo' with input.cursorSequence rewinds "
        "the shared operation history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "tool_name": {
                "type": "string",
                "description": (
                    "One of: timeline_trim, timeline_arrange, timeline_deleteClips, "
                    "timeline_text, timeline_properties, timeline_filter, timeline_keyframe, "
                    "timeline_audio, timeline_track, timeline_transition, undo."
                ),
            },
            "input": {
                "type": "object",
                "description": 'Always {"payload": {...}} — the tool\'s payload wrapped in a payload key; server-validated with precise errors.',
            },
            "expected_version": {"type": "integer", "description": "Document version from the last timeline read."},
        },
        "required": ["session_id", "tool_name"],
        "additionalProperties": False,
    },
}

CASSETTE_CANCEL_JOB = {
    "name": "cassette_cancel_job",
    "description": "Request cancellation for a persisted Cassette job. The worker observes the state and exits cleanly.",
    "parameters": {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
        "additionalProperties": False,
    },
}
