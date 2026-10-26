"""URL normalization for the Skill-enabled Harbor Claude Code bridge."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def claude_code_upstream_base_url(base_url: str | None) -> str | None:
    """Avoid a duplicate /v1 when Claude Code requests /v1/messages.

    The Harbor request proxy joins the configured upstream with Claude Code's
    full request path. An upstream ending in /v1 would otherwise receive
    /v1/v1/messages. Strip repeated terminal /v1 segments as well, because
    the local endpoint may already include one before the user adds another.
    """
    if base_url is None:
        return None
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    while path == "/v1" or path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit(parsed._replace(path=path))
