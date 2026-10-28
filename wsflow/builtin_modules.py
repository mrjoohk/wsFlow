"""Ownership of the existing HTTP surface by user-facing integration modules.

This is the compatibility adapter for the original API.  Core registry reads,
authentication, pickers and module management remain available without either
business module.  Ownership never changes an endpoint's authorization checks.
"""
from __future__ import annotations

WORK_CALENDAR = "work-calendar"
WORKSPACE_OPERATIONS = "workspace-operations"


def route_owner(method: str, path: str) -> str | None:
    """Return the builtin module owning an existing API path, or core/unknown.

    Segments use exactly the legacy dispatcher's empty-segment normalization.
    A module's removal therefore cannot be bypassed by a trailing/double slash.
    Unknown routes are left to the original dispatcher for its usual 404.
    """
    seg = [s for s in path.split("/") if s]
    if not seg or seg[0] != "api":
        return None
    seg = seg[1:]
    if not seg:
        return None
    if seg[:2] == ["v2", "calendar"]:
        return WORK_CALENDAR
    if seg[0] == "v1":
        rest = seg[1:]
        if not rest or rest[0] == "modules":
            return None
        if rest[0] == "calendar" or rest == ["workflow", "status"]:
            return WORK_CALENDAR
        if len(rest) >= 4 and rest[0] == "projects" and rest[2] == "writes":
            return {"git": WORKSPACE_OPERATIONS,
                    "memo": WORK_CALENDAR}.get(rest[3])
        if "context" in rest or "session" in rest:
            return WORKSPACE_OPERATIONS
        return None
    if seg[:2] == ["jira", "status"] and method == "GET":
        return WORK_CALENDAR
    if seg == ["roots"] and method in ("POST", "DELETE"):
        return WORKSPACE_OPERATIONS
    if seg == ["scan"] and method == "GET":
        return WORKSPACE_OPERATIONS
    if seg[0] == "deploy" and len(seg) == 2 and seg[1] in ("plan", "apply") and method == "POST":
        return WORKSPACE_OPERATIONS
    if seg == ["locks"] and method == "GET" or seg == ["locks", "clean"] and method == "POST":
        return WORKSPACE_OPERATIONS
    if seg == ["projects"]:
        return WORKSPACE_OPERATIONS if method == "POST" else None
    if seg[:1] == ["projects"] and len(seg) >= 2:
        if seg[1:] == ["new"] and method == "POST":
            return WORKSPACE_OPERATIONS
        if len(seg) == 2:
            return WORKSPACE_OPERATIONS if method in ("PATCH", "DELETE") else None
        if seg[2] == "jira":
            return WORK_CALENDAR
        if seg[2] in ("status", "log", "commit", "remotes", "rules", "locks",
                      "fetch", "pull", "push", "add"):
            return WORKSPACE_OPERATIONS
    return None
