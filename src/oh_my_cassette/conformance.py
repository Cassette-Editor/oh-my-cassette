"""`oh-my-cassette check`: verify a Cassette MCP endpoint against the bridge contract.

Meant for the backend's CI as much as for users: a backend change that breaks the contract should
fail on the backend's pull request, not in a user's session.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

import httpx
import httpx2
import mcp_types as types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from oh_my_cassette import __version__
from oh_my_cassette.bridge.files import same_origin
from oh_my_cassette.bridge.upstream import describe, protocol_refusal, require_modern
from oh_my_cassette.contract import (
    ARTIFACT_ORIGINAL,
    BRIDGE_ROLES,
    EXTENSION_ID,
    META_LOCAL_FILES,
    ROLE_UPLOAD_BEGIN,
    ROLE_UPLOAD_COMPLETE,
    SUPPORTED_CONTRACT_VERSIONS,
    ContractError,
    PrepareSpec,
    bridge_role,
    is_path_schema,
    local_files_spec,
    pointer_set,
    schema_at_pointer,
    server_contract,
    version_tuple,
)

Status = Literal["pass", "warn", "fail"]

# 1x1 transparent PNG used for the upload round trip.
PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@dataclass(frozen=True)
class Check:
    status: Status
    name: str
    detail: str


class _Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, status: Status, name: str, detail: str) -> None:
        self.checks.append(Check(status, name, detail))


async def _all_tools(client: Client) -> list[types.Tool]:
    tools: list[types.Tool] = []
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools


def _check_tools(report: _Report, tools: list[types.Tool]) -> tuple[list[types.Tool], dict[str, types.Tool]]:
    visible = [tool for tool in tools if bridge_role(tool.meta) is None]
    roles = {role: tool for tool in tools if (role := bridge_role(tool.meta)) is not None}
    for role, tool in roles.items():
        if role not in BRIDGE_ROLES:
            report.add("fail", "bridge tools", f"{tool.name} declares unknown bridge role {role!r}")
    if visible:
        report.add("pass", "tools", f"{len(visible)} host-visible tools")
    else:
        report.add("fail", "tools", "no host-visible tools")

    bad_input = [tool.name for tool in tools if tool.input_schema.get("type") != "object"]
    if bad_input:
        report.add("fail", "input schemas", f"root type is not object: {', '.join(bad_input)}")
    bad_output = [
        tool.name
        for tool in tools
        if tool.output_schema is not None and tool.output_schema.get("type") != "object"
    ]
    if bad_output:
        report.add("fail", "output schemas", f"structured output must be an object: {', '.join(bad_output)}")

    file_tools: list[types.Tool] = []
    for tool in visible:
        try:
            spec = local_files_spec(tool.meta)
        except ContractError as exc:
            name = "prepare" if str(exc).startswith(f"{META_LOCAL_FILES}.prepare") else "local files"
            report.add("fail", name, f"{tool.name}: {exc}")
            continue
        if spec is None:
            continue
        file_tools.append(tool)
        if spec.prepare is not None:
            report.add("pass", "prepare", f"{tool.name}: {_describe_prepare(spec.prepare)}")
        for pointer in spec.pointers:
            if not is_path_schema(schema_at_pointer(tool.input_schema, pointer)):
                report.add(
                    "fail", "local files", f"{tool.name}: {pointer} is not a string or string[] parameter"
                )
    if file_tools:
        missing = [role for role in (ROLE_UPLOAD_BEGIN, ROLE_UPLOAD_COMPLETE) if role not in roles]
        names = ", ".join(tool.name for tool in file_tools)
        if missing:
            report.add(
                "fail", "upload tools", f"{names} take local files but {', '.join(missing)} is missing"
            )
        else:
            report.add("pass", "local files", f"{names} declare local files; upload tools present")
    return file_tools, roles


def _describe_prepare(spec: PrepareSpec) -> str:
    parts: list[str] = []
    if spec.renditions:
        parts.append("video → " + ", ".join(rendition.role for rendition in spec.renditions))
    for kind, rule in (("audio", spec.audio), ("image", spec.image)):
        if rule is not None:
            parts.append(f"{kind} as is ({', '.join(rule.accept)}), else → {rule.convert.extension}")
    return "; ".join(parts) or "declared, nothing to prepare"


def _probe_arguments(tool: types.Tool, arguments: dict[str, Any], client_ref: str) -> dict[str, Any]:
    """The file tool's arguments with the probe's clientRef at its first local-file pointer."""
    spec = local_files_spec(tool.meta)
    args = dict(arguments)
    if spec is not None:
        pointer = spec.pointers[0]
        schema = schema_at_pointer(tool.input_schema, pointer)
        try:
            pointer_set(args, pointer, [client_ref] if (schema or {}).get("type") == "array" else client_ref)
        except ContractError:
            pass
    return args


async def _upload_round_trip(
    report: _Report,
    client: Client,
    roles: dict[str, types.Tool],
    file_tools: list[types.Tool],
    url: str,
    token: str | None,
    arguments: dict[str, Any],
) -> None:
    begin, complete = roles.get(ROLE_UPLOAD_BEGIN), roles.get(ROLE_UPLOAD_COMPLETE)
    if begin is None or complete is None:
        report.add("fail", "upload round trip", "the backend offers no upload tools")
        return
    if not file_tools:
        report.add("warn", "upload round trip", "no tool takes local files; nothing to upload")
        return
    digest = hashlib.sha256(PROBE_PNG).hexdigest()
    described = {"size": len(PROBE_PNG), "sha256": digest, "mimeType": "image/png"}
    started = await client.call_tool(
        begin.name,
        {
            "tool": file_tools[0].name,
            "arguments": _probe_arguments(file_tools[0], arguments, "probe"),
            "files": [
                {
                    "clientRef": "probe",
                    "name": "cassette-check.png",
                    "relativePath": "cassette-check.png",
                    **described,
                    "artifacts": [{"role": ARTIFACT_ORIGINAL, **described}],
                    "preparation": None,
                }
            ],
        },
    )
    uploads = (started.structured_content or {}).get("uploads") if not started.is_error else None
    entry = next((u for u in uploads or [] if isinstance(u, dict) and u.get("clientRef") == "probe"), None)
    if entry is None:
        detail = " ".join(b.text for b in started.content if isinstance(b, types.TextContent))[:300]
        report.add("fail", "upload round trip", f"{begin.name} returned no entry for the probe file {detail}")
        return
    if isinstance(entry.get("ref"), str) and not isinstance(entry.get("uploadId"), str):
        report.add("pass", "upload round trip", f"begin → dedupe gave ref {entry['ref']}")
        return
    puts = entry.get("puts")
    put = puts[0] if isinstance(puts, list) and len(puts) == 1 and isinstance(puts[0], dict) else None
    if not isinstance(entry.get("uploadId"), str) or put is None or put.get("role") != ARTIFACT_ORIGINAL:
        report.add(
            "fail",
            "upload round trip",
            f"{begin.name} must return a ref, or an uploadId with one put for role {ARTIFACT_ORIGINAL!r}",
        )
        return
    if not isinstance(put.get("url"), str) or not put["url"].startswith(("http://", "https://")):
        report.add("fail", "upload round trip", f"{begin.name} returned a put without an http(s) url")
        return
    headers = {str(k): str(v) for k, v in (put.get("headers") or {}).items()}
    headers["Content-Length"] = str(len(PROBE_PNG))
    if token and same_origin(put["url"], url):
        headers.setdefault("Authorization", f"Bearer {token}")
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.put(put["url"], content=PROBE_PNG, headers=headers)
    if response.status_code >= 300:
        report.add("fail", "upload round trip", f"PUT answered HTTP {response.status_code}")
        return
    finished = await client.call_tool(complete.name, {"uploadIds": [entry["uploadId"]]})
    files = (finished.structured_content or {}).get("files") if not finished.is_error else None
    ref = next(
        (f.get("ref") for f in files or [] if isinstance(f, dict) and f.get("uploadId") == entry["uploadId"]),
        None,
    )
    if isinstance(ref, str):
        report.add("pass", "upload round trip", f"begin → PUT → complete gave ref {ref}")
    else:
        report.add("fail", "upload round trip", f"{complete.name} returned no ref")


async def run_checks(
    url: str,
    *,
    token: str | None = None,
    upload: bool = False,
    timeout: float = 15.0,
    arguments: dict[str, Any] | None = None,
) -> list[Check]:
    report = _Report()
    headers = {"User-Agent": f"oh-my-cassette/{__version__} (check)"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    rejected: list[int] = []

    async def on_response(response: httpx2.Response) -> None:
        if response.status_code == 401:
            rejected.append(401)

    http = create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(timeout, read=timeout * 4))
    http.event_hooks = {"response": [on_response]}
    try:
        async with (
            http,
            Client(
                streamable_http_client(url, http_client=http),
                mode="auto",
                read_timeout_seconds=timeout,
                cache=None,
                client_info=types.Implementation(name="oh-my-cassette-check", version=__version__),
            ) as client,
        ):
            require_modern(client, url)
            info = client.server_info
            who = f"{info.name} {info.version}" if info else "anonymous server"
            report.add("pass", "transport", f"connected over {client.protocol_version} ({who})")
            caps = client.server_capabilities
            if caps.tools is None:
                report.add("fail", "capabilities", "the server does not offer tools")
            contract = server_contract(caps.extensions)
            if not contract.declared:
                report.add(
                    "warn",
                    "contract",
                    f"no {EXTENSION_ID} declaration in capabilities.extensions",
                )
            elif contract.version not in SUPPORTED_CONTRACT_VERSIONS:
                report.add(
                    "fail",
                    "contract",
                    f"declares version {contract.version!r}; supported: {sorted(SUPPORTED_CONTRACT_VERSIONS)}",
                )
            elif contract.min_bridge_version and version_tuple(__version__) < version_tuple(
                contract.min_bridge_version
            ):
                report.add(
                    "warn",
                    "contract",
                    f"v{contract.version}, but requires bridge >= {contract.min_bridge_version}",
                )
            else:
                report.add("pass", "contract", f"v{contract.version}")
            if client.instructions and client.instructions.strip():
                report.add("pass", "instructions", f"{len(client.instructions)} characters")
            else:
                report.add("warn", "instructions", "no instructions; hosts get no workflow guidance")
            file_tools, roles = _check_tools(report, await _all_tools(client))
            if upload:
                await _upload_round_trip(report, client, roles, file_tools, url, token, arguments or {})
    except Exception as exc:
        if refusal := protocol_refusal(exc):
            report.add("fail", "transport", str(refusal))
        elif rejected:
            hint = (
                "the token was rejected"
                if token
                else "a token is required; pass --token or set CASSETTE_AUTH_TOKEN"
            )
            report.add("fail", "transport", f"HTTP 401 Unauthorized: {hint}")
        else:
            report.add("fail", "transport", describe(exc))
    return report.checks


def render(checks: list[Check], *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps([asdict(check) for check in checks], indent=2)
    width = max((len(check.name) for check in checks), default=0)
    return "\n".join(f"{check.status.upper():<4}  {check.name:<{width}}  {check.detail}" for check in checks)


def failed(checks: list[Check]) -> bool:
    return any(check.status == "fail" for check in checks)


def summary(checks: list[Check]) -> dict[str, Any]:
    return {status: sum(1 for c in checks if c.status == status) for status in ("pass", "warn", "fail")}
