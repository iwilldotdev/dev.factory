"""`factory hook pre-tool`: the optional host hook that refuses hand edits of a run's events and evidence [L-160, L-161].
Defence in depth only: the digests, the plan lock and the gates stay authoritative [L-17]."""
from __future__ import annotations

import json
import re
import sys

COMMAND = "factory hook pre-tool"  # the installed entry point, never a path into a working tree [L-149]
ENTRY = {"matcher": "Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit|mcp__dev_mcp__.*", "hooks": [{"type": "command", "command": COMMAND}]}
PROTECTED = re.compile(r"\.factory/runs/[^/\s\"']+/(?:events\.(?:jsonl|lock)|(?:artifacts|reviews|contracts|raw)(?:/|$|[\s\"']))", re.I)
REDIRECT = re.compile(r">>?\s*[\"']?\S*" + PROTECTED.pattern, re.I)
WRITERS = re.compile(r"\b(?:tee|rm|mv|cp|sed|truncate|dd|ln|touch|install|rmdir|del|erase|ren|move|copy|python3?|node|perl|ruby)\b|"
                     r"(?:set|add|clear)-content|out-file|remove-item|move-item|copy-item|new-item", re.I)
PATH_KEYS = ("file_path", "notebook_path", "save_to", "path")


def reason(payload) -> str | None:
    """Why the tool call is refused, or None."""
    if not isinstance(payload, dict) or not isinstance(payload.get("tool_input", {}), dict):
        return "unreadable hook input"
    args = payload.get("tool_input", {})
    for key in PATH_KEYS:
        if isinstance(args.get(key), str) and PROTECTED.search(args[key].replace("\\", "/") + "/"):
            return f"{args[key]} is a run file that only the factory CLI writes"
    cmd = args.get("command")
    if isinstance(cmd, str):
        cmd = cmd.replace("\\", "/")
        if REDIRECT.search(cmd) or (PROTECTED.search(cmd) and WRITERS.search(cmd)):
            return "the command writes a run file (events, artifacts, reviews, contracts or raw) by hand"
    return None


def run(stdin_text: str) -> str:
    try:
        why = reason(json.loads(stdin_text))
    except ValueError:
        why = "unreadable hook input"
    if not why:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": f"dev.factory: {why}. Run files are written only by `factory` commands."}})


def cmd_hook(args) -> int:
    out = run(sys.stdin.read())
    if out:
        print(out)
    return 0
