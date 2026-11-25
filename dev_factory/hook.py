"""`factory hook pre-tool`: the optional host hook that refuses hand edits of a run's events and evidence [L-160, L-161].
A guard against accidents, not a sandbox: paths are normalised (`./`, `../`, `//`, backslashes, case), a `cd <run dir> && ...` and a
`$VAR/events.jsonl` target are caught on a best-effort basis, and an interpreter is refused only when it also shows a write. The digests,
the plan lock and the gates stay authoritative [L-17]. Out of scope: `pushd`, `cd <runs dir> && cp x R1/events.jsonl`, .NET file calls,
and a `cp`/`mv` that only reads a protected source (refused anyway)."""
from __future__ import annotations

import json
import re
import sys

COMMAND = "factory hook pre-tool"  # the installed entry point, never a path into a working tree [L-149]
ENTRY = {"matcher": "Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit|mcp__dev_mcp__.*", "hooks": [{"type": "command", "command": COMMAND}]}
TARGETS = r"(?:events\.(?:jsonl|lock)|(?:artifacts|reviews|contracts|raw)(?:/|$|[\s\"']))"
PROTECTED = re.compile(r"\.factory/runs/[^/\s\"']+/" + TARGETS, re.I)
VAR_TARGET = re.compile(r"[$%]\{?\w+\}?%?/" + TARGETS, re.I)  # `$P/events.jsonl`: the variable may hold the run dir
CD_RUN = re.compile(r"\bcd\s+[\"']?(\S*\.factory/runs/[^/\s\"']+)", re.I)
REDIRECT = re.compile(r">>?\s*[\"']?\S*(?:" + PROTECTED.pattern + "|" + VAR_TARGET.pattern + ")", re.I)
VERB = (r"tee|rm|mv|cp|sed|truncate|dd|ln|touch|install|rmdir|del|erase|ren|move|copy|(?:set|add|clear)-content|out-file|"
        r"remove-item|move-item|copy-item|new-item")  # a verb counts at the start of a command, not as a word inside an argument
WRITERS = re.compile(r"(?:^|[;&|(\n\"']|\b(?:sudo|xargs|env|command|exec|nohup))\s*(?:[\w.~-]*/)*(?:" + VERB + r")\b|"
                     r"\b(?:python3?|node|perl|ruby)\b.*(?:open\(|write|unlink|remove|rename|rmtree|copy|appendfile|\bfs\.|\s[\w./-]+\.(?:py|js|pl|rb)\b)", re.I)
COMMENT = re.compile(r"(\"[^\"]*\"|'[^']*')|\s#.*$", re.M)  # a `#` inside quotes is text, not a comment
PATH_KEYS = ("file_path", "notebook_path", "save_to", "path")


def norm(text: str) -> str:
    """Backslashes to slashes, `//` collapsed, `/./` and `dir/../` resolved, so a path cannot dodge the pattern by spelling."""
    text = re.sub(r"/{2,}", "/", text.replace("\\", "/")).replace("/./", "/")
    while (new := re.sub(r"[^/\s\"']+/\.\./", "", text, count=1)) != text:
        text = new
    return text


def reason(payload) -> str | None:
    """Why the tool call is refused, or None."""
    if not isinstance(payload, dict) or not isinstance(payload.get("tool_input", {}), dict):
        return "unreadable hook input"
    args = payload.get("tool_input", {})
    for key in PATH_KEYS:
        if isinstance(args.get(key), str) and PROTECTED.search(norm(args[key]) + "/"):
            return f"{args[key]} is a run file that only the factory CLI writes"
    cmd = args.get("command")
    if isinstance(cmd, str):
        cmd = norm(COMMENT.sub(lambda m: m.group(1) or "", cmd))
        if (cd := CD_RUN.search(cmd)):  # relative targets after `cd <run dir>` mean that run's files
            cmd = re.sub(r"(?<![\w/.$-])(?=" + TARGETS + ")", cd.group(1) + "/", cmd, flags=re.I)
        if REDIRECT.search(cmd) or any((PROTECTED.search(s) or VAR_TARGET.search(s)) and WRITERS.search(s) for s in re.split(r"[;&|\n]+", cmd)):
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
