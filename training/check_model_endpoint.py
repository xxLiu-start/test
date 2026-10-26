#!/usr/bin/env python3
"""Probe both gateway protocols without printing credentials or response bodies."""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener, getproxies, urlopen


MODEL = "deepseek-v4.1-flash"


def origin(base_url: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ANTHROPIC_BASE_URL must be an HTTP(S) URL")
    path = parsed.path.rstrip("/")
    while path == "/v1" or path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit(parsed._replace(path=path, query="", fragment="")).rstrip("/")


def probe(url: str, key: str, payload: dict, *, anthropic: bool = False, direct: bool = False) -> str:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if anthropic:
        headers["anthropic-version"] = "2023-06-01"
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    open_request = build_opener(ProxyHandler({})).open if direct else urlopen
    try:
        with open_request(request, timeout=45) as response:
            return f"HTTP {response.status}"
    except HTTPError as exc:
        return f"HTTP {exc.code}"
    except URLError as exc:
        return f"network error ({type(exc.reason).__name__})"


def probe_tools(url: str, key: str, *, anthropic: bool, direct: bool) -> str:
    function = {
        "name": "emit_marker",
        "description": "Return the requested marker value.",
    }
    parameters = {
        "type": "object",
        "properties": {"marker": {"type": "string"}},
        "required": ["marker"],
    }
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Call emit_marker with marker set to ok."}],
        "max_tokens": 128,
        "stream": False,
    }
    if anthropic:
        payload["tools"] = [{**function, "input_schema": parameters}]
        payload["tool_choice"] = {"type": "tool", "name": "emit_marker"}
    else:
        payload["tools"] = [{"type": "function", "function": {**function, "parameters": parameters}}]
        payload["tool_choice"] = {"type": "function", "function": {"name": "emit_marker"}}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if anthropic:
        headers["anthropic-version"] = "2023-06-01"
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    open_request = build_opener(ProxyHandler({})).open if direct else urlopen
    try:
        with open_request(request, timeout=45) as response:
            mime_type = response.headers.get_content_type()
            body = response.read(1_000_001)
            if len(body) > 1_000_000:
                return f"HTTP {response.status}; response too large to inspect"
            return inspect_tool_response(response.status, mime_type, body, anthropic=anthropic)
    except HTTPError as exc:
        return f"HTTP {exc.code}"
    except URLError as exc:
        return f"network error ({type(exc.reason).__name__})"


def inspect_tool_response(status: int, mime_type: str, body: bytes, *, anthropic: bool) -> str:
    if mime_type == "text/event-stream":
        events = []
        for line in body.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        if anthropic:
            structured = any(
                event.get("type") == "content_block_start"
                and isinstance(event.get("content_block"), dict)
                and event["content_block"].get("type") == "tool_use"
                for event in events
            )
        else:
            structured = any(
                any(isinstance(choice, dict) and (choice.get("delta") or {}).get("tool_calls") for choice in event.get("choices", []))
                for event in events
            )
        return f"HTTP {status}; SSE; structured tool call: {'yes' if structured else 'no'}"
    try:
        result = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"HTTP {status}; non-JSON response; content-type: {mime_type}"
    if not isinstance(result, dict):
        return f"HTTP {status}; JSON top-level type: {type(result).__name__}"
    keys = ",".join(sorted(str(key)[:40] for key in result)[:12])
    if anthropic:
        blocks = result.get("content")
        if not isinstance(blocks, list):
            return f"HTTP {status}; JSON keys: {keys}; content type: {type(blocks).__name__}"
        structured = any(isinstance(item, dict) and item.get("type") == "tool_use" for item in blocks)
        types = ",".join(sorted({str(item.get("type")) for item in blocks if isinstance(item, dict)}))
        return f"HTTP {status}; structured tool call: {'yes' if structured else 'no'}; content types: {types or 'none'}"
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return f"HTTP {status}; JSON keys: {keys}; choices type: {type(choices).__name__}"
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return f"HTTP {status}; JSON keys: {keys}; message type: {type(message).__name__}"
    return f"HTTP {status}; structured tool call: {'yes' if message.get('tool_calls') else 'no'}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct", action="store_true", help="Bypass HTTP(S) proxy environment variables")
    parser.add_argument("--tools", action="store_true", help="Check whether each protocol honors a forced structured tool call")
    args = parser.parse_args()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    if not key or not base_url:
        print("Set ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL in this shell first.", file=sys.stderr)
        return 2
    gateway = origin(base_url)
    if args.direct:
        print("Connection route: direct (proxy bypassed)")
    else:
        proxy_url = getproxies().get("https") or getproxies().get("all")
        if proxy_url:
            proxy = urlsplit(proxy_url)
            print(f"Connection route: HTTPS proxy {proxy.hostname}:{proxy.port or 'default'}")
        else:
            print("Connection route: direct (no HTTPS proxy configured)")
    messages = [{"role": "user", "content": "Reply OK."}]
    if args.tools:
        chat = probe_tools(f"{gateway}/v1/chat/completions", key, anthropic=False, direct=args.direct)
        anthropic = probe_tools(f"{gateway}/v1/messages", key, anthropic=True, direct=args.direct)
        print(f"OpenAI Chat Completions: {chat}")
        print(f"Anthropic Messages: {anthropic}")
        return 0 if "structured tool call: yes" in chat and "structured tool call: yes" in anthropic else 1
    chat = probe(
        f"{gateway}/v1/chat/completions", key,
        {"model": MODEL, "messages": messages, "max_tokens": 8, "stream": False},
        direct=args.direct,
    )
    anthropic = probe(
        f"{gateway}/v1/messages", key,
        {"model": MODEL, "messages": messages, "max_tokens": 8, "stream": False},
        anthropic=True, direct=args.direct,
    )
    print(f"OpenAI Chat Completions: {chat}")
    print(f"Anthropic Messages: {anthropic}")
    return 0 if chat.startswith("HTTP 2") and anthropic.startswith("HTTP 2") else 1


if __name__ == "__main__":
    raise SystemExit(main())
