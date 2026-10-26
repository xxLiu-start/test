#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

REQUEST_HEADERS_TO_SKIP = HOP_BY_HOP_HEADERS | {"accept-encoding"}

REQUEST_AUDIT_KEYS = (
    "model",
    "max_tokens",
    "temperature",
    "top_p",
    "top_k",
    "enable_thinking",
    "thinking",
    "stream",
)


class ProxyConfig:
    def __init__(
        self,
        *,
        upstream: str,
        overrides: dict[str, Any],
        dashscope_wait_timeout: str | None,
        log_file: Path | None,
        requests_log_file: Path | None,
        model_io_file: Path | None = None,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.overrides = overrides
        self.dashscope_wait_timeout = dashscope_wait_timeout
        self.log_file = log_file
        self.requests_log_file = requests_log_file
        self.model_io_file = model_io_file
        self.lock = threading.Lock()
        # Claude Code omits OpenAI reasoning_content from its Anthropic history.
        # Keep it only for this proxy process so tool continuations can restore
        # DeepSeek's required reasoning context without exposing it in logs.
        self.reasoning_by_tool_id: dict[str, str] = {}

    def log(self, message: str) -> None:
        line = f"[claude-code-proxy] {message}\n"
        with self.lock:
            if self.log_file is not None:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                with self.log_file.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            else:
                sys.stderr.write(line)

    def log_request_event(self, event: dict[str, Any]) -> None:
        if self.requests_log_file is None:
            return
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with self.lock:
            self.requests_log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.requests_log_file.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def record_model_io(self, event: dict[str, Any]) -> None:
        """Best-effort, view-only trace; never send this file back to a model."""
        if self.model_io_file is None:
            return
        record = {"schema_version": 1, "timestamp": datetime.now(timezone.utc).isoformat(), **event}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            with self.lock:
                self.model_io_file.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.model_io_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError as exc:
            self.log(f"model I/O trace write failed: {type(exc).__name__}")


class RequestProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    config: ProxyConfig

    def log_message(self, fmt: str, *args: Any) -> None:
        self.config.log(fmt % args)

    def do_GET(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        if self.path == "/":
            self.send_response(200)
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        body = self._read_body()
        trace_id = uuid.uuid4().hex
        self.config.record_model_io({
            "event": "client_request", "trace_id": trace_id,
            "method": self.command, "path": self.path,
            "payload": structured_body(body),
        })
        self.config.log(f"forwarding {self.command} {self.path}")

        upstream_url = urllib.parse.urljoin(self.config.upstream + "/", self.path.lstrip("/"))
        parsed = urllib.parse.urlparse(upstream_url)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query

        body, changed, audit = rewrite_body(
            body,
            overrides=self.config.overrides,
        )
        if audit is not None:
            self.config.log_request_event(
                {
                    "event": "request",
                    "path": self.path,
                    "upstream": upstream_url,
                    "before": audit["before"],
                    "after": audit["after"],
                }
            )
        if changed:
            self.config.log(f"rewrote request body for {self.path}")

        headers = self._forward_headers(body)
        self.config.log_request_event(
            {
                "event": "forwarded_auth",
                "path": self.path,
                "bearer": any(
                    key.lower() == "authorization" and value.startswith("Bearer ")
                    for key, value in headers.items()
                ),
                "x_api_key": any(key.lower() == "x-api-key" for key in headers),
            }
        )
        if (
            os.environ.get("LH_HARNESS_PROXY_OPENAI_BRIDGE") == "1"
            and self.command == "POST"
            and self.path.split("?", 1)[0] == "/v1/messages"
        ):
            self._forward_openai_bridge(body, headers, trace_id=trace_id)
            return
        self.config.record_model_io({
            "event": "model_input", "trace_id": trace_id,
            "protocol": "anthropic_messages", "upstream": upstream_url,
            "payload": structured_body(body),
        })
        conn = make_connection(parsed)
        try:
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in HOP_BY_HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                self.close_connection = True
                return
            error_body_head = bytearray()
            response_body = bytearray()
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                response_body.extend(chunk)
                if resp.status >= 400 and len(error_body_head) < 4096:
                    remaining = 4096 - len(error_body_head)
                    error_body_head.extend(chunk[:remaining])
                self.wfile.write(chunk)
                self.wfile.flush()
            self.config.record_model_io({
                "event": "model_output", "trace_id": trace_id,
                "protocol": "anthropic_messages", "status": resp.status,
                "payload": structured_body(bytes(response_body)),
            })
            if resp.status >= 400:
                self.config.log_request_event(
                    {
                        "event": "upstream_http_error",
                        "path": self.path,
                        "upstream": upstream_url,
                        "status": resp.status,
                        "reason": resp.reason,
                        "body_head": error_body_head.decode("utf-8", errors="replace"),
                    }
                )
            self.close_connection = True
        except Exception as exc:
            self.config.record_model_io({
                "event": "model_error", "trace_id": trace_id,
                "protocol": "anthropic_messages", "error_type": type(exc).__name__,
            })
            self.config.log(f"upstream request failed: {exc!r}")
            self.config.log_request_event(
                {
                    "event": "upstream_error",
                    "path": self.path,
                    "upstream": upstream_url,
                    "error": repr(exc),
                }
            )
            payload = json.dumps(
                {"type": "error", "error": {"type": "proxy_error", "message": str(exc)}}
            ).encode("utf-8")
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
        finally:
            conn.close()

    def _forward_openai_bridge(self, body: bytes, headers: dict[str, str], *, trace_id: str | None = None) -> None:
        trace_id = trace_id or uuid.uuid4().hex
        try:
            request = json.loads(body)
            if not isinstance(request, dict):
                raise ValueError("Messages request is not a JSON object")
            with self.config.lock:
                reasoning_snapshot = dict(self.config.reasoning_by_tool_id)
            openai_request = anthropic_to_openai(request, reasoning_by_tool_id=reasoning_snapshot)
            upstream_url = self.config.upstream + "/v1/chat/completions"
            parsed = urllib.parse.urlparse(upstream_url)
            translated_body = json.dumps(openai_request, ensure_ascii=False).encode("utf-8")
            self.config.record_model_io({
                "event": "model_input", "trace_id": trace_id,
                "protocol": "openai_chat_completions", "upstream": upstream_url,
                "payload": openai_request,
                "raw_text": translated_body.decode("utf-8"),
            })
            authorization = next(
                (value for key, value in headers.items() if key.lower() == "authorization"), None,
            )
            if not authorization:
                raise ValueError("OpenAI bridge requires Bearer authorization")
            translated_headers = {
                "Authorization": authorization,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Content-Length": str(len(translated_body)),
            }
            self.config.log_request_event({
                "event": "openai_bridge_request", "path": self.path,
                "upstream": upstream_url,
                "tools": len(openai_request.get("tools", [])),
                "messages": len(openai_request["messages"]),
            })
            conn = make_connection(parsed)
            try:
                conn.request("POST", parsed.path, body=translated_body, headers=translated_headers)
                resp = conn.getresponse()
                raw = resp.read()
                self.config.record_model_io({
                    "event": "model_output", "trace_id": trace_id,
                    "protocol": "openai_chat_completions", "status": resp.status,
                    "payload": structured_body(raw),
                })
                if resp.status >= 400:
                    self.config.log_request_event({
                        "event": "upstream_http_error", "path": self.path,
                        "upstream": upstream_url, "status": resp.status,
                        "body_head": raw[:4096].decode("utf-8", errors="replace"),
                    })
                    self._send_bridge_bytes(resp.status, raw, "application/json")
                    return
                completion = json.loads(raw)
                new_reasoning: dict[str, str] = {}
                message = openai_to_anthropic(
                    completion, request["model"], reasoning_by_tool_id=new_reasoning,
                )
                if new_reasoning:
                    with self.config.lock:
                        self.config.reasoning_by_tool_id.update(new_reasoning)
                self.config.log_request_event({
                    "event": "openai_bridge_response", "path": self.path,
                    "content_types": [block["type"] for block in message["content"]],
                    "stop_reason": message["stop_reason"],
                })
                if request.get("stream"):
                    client_body = anthropic_sse(message)
                    self.config.record_model_io({
                        "event": "client_response", "trace_id": trace_id,
                        "protocol": "anthropic_messages", "status": 200,
                        "payload": structured_body(client_body),
                    })
                    self._send_bridge_bytes(200, client_body, "text/event-stream")
                else:
                    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
                    self.config.record_model_io({
                        "event": "client_response", "trace_id": trace_id,
                        "protocol": "anthropic_messages", "status": 200,
                        "payload": message, "raw_text": payload.decode("utf-8"),
                    })
                    self._send_bridge_bytes(200, payload, "application/json")
            finally:
                conn.close()
        except (ValueError, TypeError, KeyError, OSError, http.client.HTTPException) as exc:
            self.config.record_model_io({
                "event": "model_error", "trace_id": trace_id,
                "protocol": "openai_chat_completions", "error_type": type(exc).__name__,
                "error": str(exc),
            })
            self.config.log_request_event({
                "event": "openai_bridge_error", "path": self.path,
                "error_type": type(exc).__name__,
            })
            payload = json.dumps({
                "type": "error",
                "error": {"type": "proxy_error", "message": f"OpenAI bridge failed: {type(exc).__name__}: {exc}"},
            }).encode("utf-8")
            self._send_bridge_bytes(502, payload, "application/json")

    def _send_bridge_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def _forward_headers(self, body: bytes) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if key.lower() not in REQUEST_HEADERS_TO_SKIP:
                headers[key] = value
        # Some OpenAI-compatible gateways accept Anthropic Messages but require
        # the same Bearer authorization used by their OpenAI endpoint. Claude
        # Code sends ANTHROPIC_API_KEY as x-api-key; translate it only when the
        # training/evaluation configuration explicitly requests this mode.
        if os.environ.get("LH_HARNESS_PROXY_BEARER_AUTH") == "1":
            api_key = next(
                (value for key, value in headers.items() if key.lower() == "x-api-key"),
                None,
            )
            headers = {key: value for key, value in headers.items() if key.lower() != "x-api-key"}
            if api_key:
                headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
                headers["Authorization"] = f"Bearer {api_key}"
        headers["accept-encoding"] = "identity"
        if self.config.dashscope_wait_timeout:
            headers["X-DashScope-Wait-Timeout"] = self.config.dashscope_wait_timeout
        if body:
            headers["content-length"] = str(len(body))
        return headers


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        raise ValueError("unsupported non-text content block")
    if not isinstance(content, list):
        raise ValueError("unsupported message content")
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            raise ValueError("unsupported non-text content block")
        parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def structured_body(body: bytes) -> Any:
    """Preserve exact response text and parsed JSON/SSE events for inspection."""
    if not body:
        return None
    raw_text = body.decode("utf-8", errors="replace")
    try:
        return {"format": "json", "value": json.loads(raw_text), "raw_text": raw_text}
    except json.JSONDecodeError:
        pass
    events = []
    current: dict[str, Any] = {}
    for line in raw_text.splitlines():
        if not line:
            if current:
                events.append(current)
                current = {}
        elif line.startswith("event:"):
            current["event"] = line[6:].strip()
        elif line.startswith("data:"):
            raw_data = line[5:].strip()
            try:
                current["data"] = json.loads(raw_data)
            except json.JSONDecodeError:
                current["data"] = raw_data
    if current:
        events.append(current)
    if events:
        return {"format": "sse", "events": events, "raw_text": raw_text}
    return {"format": "text", "raw_text": raw_text}


def anthropic_to_openai(
    request: dict[str, Any], *, reasoning_by_tool_id: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Translate Claude Code's text/tool message subset to Chat Completions."""
    messages: list[dict[str, Any]] = []
    system = request.get("system")
    if system:
        messages.append({"role": "system", "content": _text_content(system)})
    for message in request.get("messages", []):
        if not isinstance(message, dict):
            raise ValueError("invalid Messages history")
        role = message.get("role")
        content = message.get("content")
        if role in {"system", "developer"}:
            # Claude Code can place system/developer context in the history as
            # well as in the top-level system field. Chat Completions accepts
            # these as system messages in their original order.
            messages.append({"role": "system", "content": _text_content(content)})
        elif role == "assistant":
            if isinstance(content, str):
                messages.append({"role": "assistant", "content": content})
                continue
            if not isinstance(content, list):
                raise ValueError("invalid assistant content")
            text_parts = []
            tool_calls = []
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("invalid assistant block")
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block["id"], "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    })
                elif block.get("type") not in {"thinking", "redacted_thinking"}:
                    raise ValueError(f"unsupported assistant block: {block.get('type')}")
            item: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if tool_calls:
                item["tool_calls"] = tool_calls
                if reasoning_by_tool_id:
                    reasoning = next(
                        (reasoning_by_tool_id[call["id"]] for call in tool_calls
                         if call["id"] in reasoning_by_tool_id), None,
                    )
                    if reasoning:
                        item["reasoning_content"] = reasoning
            messages.append(item)
        elif role == "user":
            if isinstance(content, str):
                messages.append({"role": "user", "content": content})
                continue
            if not isinstance(content, list):
                raise ValueError("invalid user content")
            text_parts = []
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("invalid user block")
                kind = block.get("type")
                if kind == "text":
                    text_parts.append(str(block.get("text", "")))
                elif kind == "tool_result":
                    if text_parts:
                        messages.append({"role": "user", "content": "\n".join(text_parts)})
                        text_parts = []
                    messages.append({
                        "role": "tool", "tool_call_id": block["tool_use_id"],
                        "content": _text_content(block.get("content") or ""),
                    })
                else:
                    raise ValueError(f"unsupported user block: {kind}")
            if text_parts:
                messages.append({"role": "user", "content": "\n".join(text_parts)})
        elif role == "tool":
            tool_call_id = message.get("tool_call_id") or message.get("tool_use_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise ValueError("tool message is missing tool_call_id")
            messages.append({
                "role": "tool", "tool_call_id": tool_call_id,
                "content": _text_content(content),
            })
        else:
            raise ValueError(f"unsupported message role: {role}")
    translated: dict[str, Any] = {
        "model": request["model"], "messages": messages,
        "max_tokens": request.get("max_tokens", 4096), "stream": False,
    }
    tools = request.get("tools") or []
    if tools:
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or "name" not in tool or "input_schema" not in tool
            for tool in tools
        ):
            raise ValueError("unsupported tool definition in Messages request")
        translated["tools"] = [{
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        } for tool in tools]
    choice = request.get("tool_choice")
    if isinstance(choice, dict):
        if choice.get("type") == "tool":
            translated["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        elif choice.get("type") == "any":
            translated["tool_choice"] = "required"
        elif choice.get("type") in {"auto", "none"}:
            translated["tool_choice"] = choice["type"]
    # DeepSeek rejects forced tool choices in thinking mode.
    if translated.get("tool_choice") == "required" or isinstance(translated.get("tool_choice"), dict):
        translated["thinking"] = {"type": "disabled"}
    if request.get("stop_sequences"):
        translated["stop"] = request["stop_sequences"]
    for key in ("temperature", "top_p"):
        if request.get(key) is not None:
            translated[key] = request[key]
    return translated


def openai_to_anthropic(
    completion: dict[str, Any], model: str, *, reasoning_by_tool_id: dict[str, str] | None = None,
) -> dict[str, Any]:
    choice = completion["choices"][0]
    response = choice["message"]
    blocks: list[dict[str, Any]] = []
    content = response.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                blocks.append({"type": "text", "text": str(part.get("text", ""))})
    for call in response.get("tool_calls") or []:
        if call.get("type") != "function":
            raise ValueError("unsupported OpenAI tool-call type")
        function = call["function"]
        arguments = json.loads(function.get("arguments") or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("tool-call arguments must be a JSON object")
        tool_id = "toolu_" + uuid.uuid4().hex
        blocks.append({
            "type": "tool_use", "id": tool_id,
            "name": function["name"], "input": arguments,
        })
        reasoning = response.get("reasoning_content")
        if reasoning_by_tool_id is not None and isinstance(reasoning, str) and reasoning:
            reasoning_by_tool_id[tool_id] = reasoning
    finish = choice.get("finish_reason")
    stop_reason = "tool_use" if any(block["type"] == "tool_use" for block in blocks) else (
        "max_tokens" if finish == "length" else "end_turn"
    )
    usage = completion.get("usage") or {}
    return {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message", "role": "assistant", "content": blocks,
        "model": model, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def anthropic_sse(message: dict[str, Any]) -> bytes:
    events: list[bytes] = []

    def emit(event: str, value: dict[str, Any]) -> None:
        events.append((f"event: {event}\ndata: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8"))

    start = dict(message)
    start["content"] = []
    start["stop_reason"] = None
    start["usage"] = {"input_tokens": message["usage"]["input_tokens"], "output_tokens": 0}
    emit("message_start", {"type": "message_start", "message": start})
    for index, block in enumerate(message["content"]):
        if block["type"] == "text":
            emit("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            emit("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block["text"]}})
        else:
            emit("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
            emit("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"], ensure_ascii=False)}})
        emit("content_block_stop", {"type": "content_block_stop", "index": index})
    emit("message_delta", {"type": "message_delta", "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None}, "usage": {"output_tokens": message["usage"]["output_tokens"]}})
    emit("message_stop", {"type": "message_stop"})
    return b"".join(events)


def make_connection(parsed: urllib.parse.ParseResult) -> http.client.HTTPConnection:
    host = parsed.hostname
    if not host:
        raise ValueError(f"Invalid upstream URL: {parsed.geturl()}")
    port = parsed.port
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        return http.client.HTTPSConnection(host, port=port, timeout=600, context=context)
    if parsed.scheme == "http":
        return http.client.HTTPConnection(host, port=port, timeout=600)
    raise ValueError(f"Unsupported upstream scheme: {parsed.scheme}")


def rewrite_body(
    body: bytes,
    *,
    overrides: dict[str, Any],
) -> tuple[bytes, bool, dict[str, dict[str, Any]] | None]:
    if not body:
        return body, False, None

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body, False, None

    if not isinstance(payload, dict):
        return body, False, None

    changed = False
    next_payload = dict(payload)
    before = request_audit_payload(payload)

    for key, value in overrides.items():
        if value is None:
            continue
        if next_payload.get(key) != value:
            next_payload[key] = value
            changed = True

    after = request_audit_payload(next_payload)
    audit = {"before": before, "after": after}
    if not changed:
        return body, False, audit

    return (
        json.dumps(next_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        True,
        audit,
    )


def request_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in REQUEST_AUDIT_KEYS}


def parse_overrides(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}

    out: dict[str, Any] = {}
    for key, value in parsed.items():
        if key == "model_info" or value is None:
            continue
        if key == "extra_body" and isinstance(value, dict):
            for extra_key, extra_value in value.items():
                if extra_value is not None:
                    out[str(extra_key)] = extra_value
            continue
        out[str(key)] = value
    return out


def parse_dashscope_wait_timeout(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return str(value)


def write_ready_file(path: Path, host: str, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"ANTHROPIC_BASE_URL='http://{host}:{port}'\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--log-file")
    parser.add_argument("--requests-log-file")
    parser.add_argument("--model-io-file")
    parser.add_argument("--overrides", default="")
    parser.add_argument("--dashscope-wait-timeout", default="")
    args = parser.parse_args()

    RequestProxyHandler.config = ProxyConfig(
        upstream=args.upstream,
        overrides=parse_overrides(args.overrides),
        dashscope_wait_timeout=parse_dashscope_wait_timeout(args.dashscope_wait_timeout),
        log_file=Path(args.log_file) if args.log_file else None,
        requests_log_file=Path(args.requests_log_file) if args.requests_log_file else None,
        model_io_file=(
            Path(args.model_io_file)
            if args.model_io_file and os.environ.get("LH_HARNESS_MODEL_IO_TRACE") == "1"
            else None
        ),
    )

    server = ThreadingHTTPServer((args.listen_host, args.port), RequestProxyHandler)
    host, port = server.server_address[:2]
    write_ready_file(Path(args.ready_file), str(host), int(port))
    RequestProxyHandler.config.log(
        f"listening on {host}:{port}, upstream={args.upstream}, "
        f"dashscope_wait_timeout={RequestProxyHandler.config.dashscope_wait_timeout or 'disabled'}"
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
