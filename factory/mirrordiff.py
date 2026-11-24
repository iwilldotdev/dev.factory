"""Mirror diff gate (DESIGN section 9): a hotfix mirror carries the SAME CHANGES as the base fix.

Per code repo and mirror target, the net change of the base branch (merge-base(base target, HEAD)..HEAD of the base
worktree) is compared with the net change of the mirror branch. Commits and branches may differ; the changed lines may
not. A recorded user decision ("mirror diff: <reason>") can allow one named divergence (`run["mirror_allow"]`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from .core import (FactoryError, code_repos, event, git, hotfix_of, now, repo_path_for_run, run_base_branch,
                    target_branch, target_worktree)

PREFIX = "mirror diff:"
EXCERPT_LINES = 6        # lines shown per hunk
EXCERPT_WIDTH = 120      # characters kept per line
EXCERPT_HUNKS = 3        # hunks shown per file and side
ERROR_FILES = 10         # files named in one gate failure

# Relative import/require paths: "./x", '../y/z' in quotes; python `from .pkg import`.
_REL_QUOTED = re.compile(r"""(['"`])\.{1,2}/[^'"`\n]*\1""")
_REL_PY = re.compile(r"\bfrom\s+\.+[\w.]*\s+import\b")


def _norm_line(line: str, relaxed: bool) -> str:
    """Exact text, except (relaxed, file at another path on the other side) relative import paths are masked.
    Whitespace is never touched: a reformat must show."""
    if not relaxed:
        return line
    line = _REL_QUOTED.sub(lambda m: f"{m.group(1)}<rel>{m.group(1)}", line)
    return _REL_PY.sub("from <rel> import", line)


def _resolve(path, ref: str) -> str:
    from .commands import gate
    return gate.resolve_base(path, ref)


def _parse_diff(text: str) -> dict[str, list[tuple[tuple[str, ...], tuple[str, ...]]]]:
    """{path: [(removed lines, added lines), ...]} from `git diff --no-renames -U0`; one entry per -U0 hunk.
    Binary files are one hunk holding the new blob id."""
    files: dict[str, list] = {}
    cur = None
    removed: list[str] = []
    added: list[str] = []
    last = None

    def flush() -> None:
        nonlocal removed, added
        if cur is not None and (removed or added):
            files[cur].append((tuple(removed), tuple(added)))
        removed, added = [], []

    for raw in text.split("\n"):
        if raw.startswith("diff --git a/"):
            flush()
            rest = raw[len("diff --git a/"):]
            n = (len(rest) - 3) // 2  # no renames: "a/<p> b/<p>"
            cur = rest[:n]
            files.setdefault(cur, [])
            last = None
        elif cur is None:
            continue
        elif raw.startswith("@@"):
            flush()
            last = None
        elif raw.startswith("index "):
            files[cur].append(((), ("BLOBID " + raw.split()[1].split("..")[-1],)))  # replaced if the file has text hunks
        elif raw.startswith("Binary files"):
            continue
        elif raw.startswith("\\"):
            if last is not None:
                last[-1] += "\\NONL"
        elif raw.startswith("+++") or raw.startswith("---"):
            continue
        elif raw.startswith("-"):
            removed.append(raw[1:])
            last = removed
        elif raw.startswith("+"):
            added.append(raw[1:])
            last = added
    flush()
    # a text file has real hunks after its `index` record: drop the blob placeholder then
    for path, hunks in files.items():
        real = [h for h in hunks if not (h[0] == () and len(h[1]) == 1 and h[1][0].startswith("BLOBID "))]
        placeholders = [h for h in hunks if h not in real]
        if real:
            files[path] = real
        elif placeholders:
            files[path] = placeholders[-1:]
    return files


def _net_diff(wt, base_ref: str) -> dict:
    mb = git(wt, "merge-base", base_ref, "HEAD")
    out = git(wt, "-c", "core.quotepath=off", "diff", "--no-renames", "--no-color", "--no-ext-diff", "-U0",
              "--src-prefix=a/", "--dst-prefix=b/", mb, "HEAD")
    return _parse_diff(out)


def _renames(wt, base_ref: str, mirror_ref: str) -> dict[str, str]:
    """{path on the mirror target: path on the base target} for files moved between the two target branches."""
    out = git(wt, "-c", "core.quotepath=off", "-c", "diff.renameLimit=10000", "diff", "--name-status", "-M",
              "--diff-filter=R", "-z", base_ref, mirror_ref, check=False)
    tok = out.split("\0")
    moved = {}
    for i in range(0, len(tok) - 2, 3):  # -z: status, old, new
        if tok[i].startswith("R"):
            moved[tok[i + 2]] = tok[i + 1]
    return moved


def _excerpt(hunks: list) -> list[str]:
    out = []
    for removed, added in hunks[:EXCERPT_HUNKS]:
        lines = [("-" + l) for l in removed] + [("+" + l) for l in added]
        shown = [l if len(l) <= EXCERPT_WIDTH else l[:EXCERPT_WIDTH] + "..." for l in lines[:EXCERPT_LINES]]
        if len(lines) > EXCERPT_LINES:
            shown.append(f"... (+{len(lines) - EXCERPT_LINES} more line(s))")
        out.append("\n".join(shown))
    if len(hunks) > EXCERPT_HUNKS:
        out.append(f"... (+{len(hunks) - EXCERPT_HUNKS} more hunk(s))")
    return out


def _lines(hunks: list) -> tuple[Counter, Counter]:
    rem, add = Counter(), Counter()
    for r, a in hunks:
        rem.update(r)
        add.update(a)
    return rem, add


def compare_files(base: dict, mirror: dict, moved: dict[str, str]) -> list[dict]:
    """Divergent files between two parsed net diffs. `moved` maps mirror-target paths to base-target paths."""
    keyed: dict[str, tuple[str, list]] = {}
    for path, hunks in mirror.items():
        keyed[moved.get(path, path)] = (path, hunks)
    out = []
    for key in sorted(set(base) | set(keyed)):
        in_b, in_m = key in base, key in keyed
        if in_b and not in_m:
            out.append({"file": key, "mirror_file": None, "kind": "only_base", "base_only": list(base[key]), "mirror_only": []})
            continue
        if in_m and not in_b:
            out.append({"file": key, "mirror_file": keyed[key][0], "kind": "only_mirror", "base_only": [],
                        "mirror_only": list(keyed[key][1])})
            continue
        mpath, mh = keyed[key]
        relaxed = mpath != key
        bh = [(tuple(_norm_line(l, relaxed) for l in r), tuple(_norm_line(l, relaxed) for l in a)) for r, a in base[key]]
        mh = [(tuple(_norm_line(l, relaxed) for l in r), tuple(_norm_line(l, relaxed) for l in a)) for r, a in mh]
        cb, cm = Counter(bh), Counter(mh)
        if cb == cm or _lines(bh) == _lines(mh):  # same hunks, or the same lines grouped differently by diff
            continue
        out.append({"file": key, "mirror_file": mpath if relaxed else None, "kind": "hunks",
                    "base_only": list((cb - cm).elements()), "mirror_only": list((cm - cb).elements())})
    return out


def compute(run: dict, ws: dict) -> dict | None:
    """The mirror diff report, or None when the run has nothing to compare (not a hotfix, no mirror created)."""
    hf = hotfix_of(run)
    if not hf or not hf.get("mirror"):
        return None
    entries = []
    for alias, rec in code_repos(run).items():
        base_wt = repo_path_for_run(run, ws, alias)
        for target in hf["mirror"]:
            mirror_wt = target_worktree(run, ws, alias, target)
            if mirror_wt is None or not base_wt.is_dir() or not mirror_wt.is_dir():
                continue
            entry = {"repo": alias, "target": target, "files": []}
            try:
                base_ref = _resolve(base_wt, run_base_branch(run, ws, alias))
                mirror_ref = _resolve(mirror_wt, target_branch(ws, alias, target))
                moved = _renames(base_wt, base_ref, mirror_ref)
                base = _net_diff(base_wt, base_ref)
                mirror = _net_diff(mirror_wt, mirror_ref)
                entry["files"] = compare_files(base, mirror, moved)
            except FactoryError as err:
                entry["error"] = str(err)
            entries.append(entry)
    if not entries:
        return None
    digest_src = [[e["repo"], e["target"], e.get("error"),
                   [[f["file"], f["kind"], sorted(map(list, f["base_only"])), sorted(map(list, f["mirror_only"]))]
                    for f in e["files"]]] for e in entries]
    digest = hashlib.sha256(json.dumps(digest_src, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    divergent = any(e["files"] or e.get("error") for e in entries)
    return {"equal": not divergent, "digest": digest, "entries": entries}


def public(report: dict) -> dict:
    """JSON-safe report: hunks become capped excerpts."""
    entries = []
    for e in report["entries"]:
        files = [{"file": f["file"], "mirror_file": f["mirror_file"], "kind": f["kind"],
                  "base_only": _excerpt(f["base_only"]), "mirror_only": _excerpt(f["mirror_only"]),
                  "base_only_hunks": len(f["base_only"]), "mirror_only_hunks": len(f["mirror_only"])} for f in e["files"]]
        entries.append({"repo": e["repo"], "target": e["target"], "files": files, **({"error": e["error"]} if e.get("error") else {})})
    return {"equal": report["equal"], "digest": report["digest"], "entries": entries}


def _file_line(f: dict) -> str:
    where = f["file"] + (f" (mirror path {f['mirror_file']})" if f.get("mirror_file") and f["mirror_file"] != f["file"] else "")
    if f["kind"] == "only_base":
        return f"{where}: changed only on the base branch (missing on the mirror)"
    if f["kind"] == "only_mirror":
        return f"{where}: changed only on the mirror branch (not in the base fix)"
    return f"{where}: {f['base_only_hunks']} hunk(s) only on base, {f['mirror_only_hunks']} only on mirror"


def render(report: dict) -> list[str]:
    pub = public(report)
    lines = []
    for e in pub["entries"]:
        label = f"{e['repo']}/{e['target']}"
        if e.get("error"):
            lines.append(f"{label}: cannot compare: {e['error']}")
        for f in e["files"]:
            lines.append(f"{label}: {_file_line(f)}")
            for side, key in (("base only", "base_only"), ("mirror only", "mirror_only")):
                for ex in f[key]:
                    lines.append(f"    {side}:")
                    lines.extend("      " + l for l in ex.split("\n"))
    return lines


# ---------- user exception ----------

def _decision(run: dict, n) -> dict:
    decisions = run.get("decisions") or []
    if not isinstance(n, int) or not 0 <= n < len(decisions):
        raise FactoryError(f"no decision #{n}")
    d = decisions[n]
    if d.get("by") != "user":
        raise FactoryError(f"decision #{n} is not a user decision (record it with --by user --user-said \"<quote>\")")
    text = str(d.get("text") or "").strip()
    if not text.casefold().startswith(PREFIX) or not text[len(PREFIX):].strip():
        raise FactoryError(f"decision #{n} must read \"{PREFIX} <reason>\" (got {text!r})")
    return d


def record_allowance(run: dict, ws: dict, n) -> None:
    """Store, on the run, the user decision #n as the allowance of the CURRENT divergence (mutates `run`; the caller saves)."""
    d = _decision(run, n)
    report = compute(run, ws)
    if report is None or report["equal"]:
        raise FactoryError("no mirror divergence to allow (the mirror diff is equal or no mirror exists yet)")
    files: dict[str, list[str]] = {}
    for e in report["entries"]:
        names = [f["file"] for f in e["files"]]
        if names:
            files[f"{e['repo']}/{e['target']}"] = names
    text = str(d["text"]).strip()
    run["mirror_allow"] = {"decision": n, "reason": text[len(PREFIX):].strip(), "digest": report["digest"],
                           "files": files, "at": now()}
    event(run, "mirror_allow", decision=n, digest=report["digest"], files=files)


def allowance_valid(run: dict, report: dict) -> bool:
    allow = run.get("mirror_allow")
    if not isinstance(allow, dict) or allow.get("digest") != report["digest"]:
        return False
    try:
        _decision(run, allow.get("decision"))
    except FactoryError:
        return False
    return True


def errors(run: dict, ws: dict) -> list[str]:
    """Gate errors: empty for non-hotfix runs, uncreated mirrors, an equal diff, or a divergence the user allowed."""
    report = compute(run, ws)
    if report is None or report["equal"] or allowance_valid(run, report):
        return []
    out = []
    shown = 0
    for e in report["entries"]:
        label = f"{e['repo']}/{e['target']}"
        if e.get("error"):
            out.append(f"{label}: mirror diff cannot be computed: {e['error']}")
        for f in e["files"]:
            if shown >= ERROR_FILES:
                break
            shown += 1
            ex = ""
            for side, key in (("only on base", "base_only"), ("only on mirror", "mirror_only")):
                hunks = f[key][:1]
                if hunks:
                    ex += f" [{side}: " + " / ".join(_excerpt(hunks)[0].split("\n")[:3]) + "]"
            out.append(f"{label}: mirror diff: {_file_line({**f, 'base_only_hunks': len(f['base_only']), 'mirror_only_hunks': len(f['mirror_only'])})}{ex}")
    total = sum(len(e["files"]) for e in report["entries"])
    if total > shown:
        out.append(f"... {total - shown} more divergent file(s); `factory run mirror-diff <run>` lists all")
    allow = run.get("mirror_allow")
    stale = " (the allowance recorded earlier no longer matches this divergence)" if isinstance(allow, dict) else ""
    out.append("the mirror must carry the same changes as the base fix (adapt only moved files/import paths; no reformatting, "
               "no relocated tests): fix the mirror branch, or with the user's explicit decision `factory run decide <run> "
               f"\"{PREFIX} <reason>\" --by user --user-said \"<quote>\"` and --allow-mirror-divergence <n>{stale}")
    return out


def description_lines(run: dict, alias: str, target: str) -> list[str]:
    """MR description section naming the user-approved divergence, for the mirror target's MR only."""
    hf = hotfix_of(run)
    allow = run.get("mirror_allow")
    if not hf or target == hf["base"] or not isinstance(allow, dict):
        return []
    files = (allow.get("files") or {}).get(f"{alias}/{target}")
    if not files:
        return []
    return (["", "## Mirror divergence (approved by the user)", "",
             f"This mirror does not carry the same diff as {hf['base']}. Reason: {allow.get('reason', '')} "
             f"(decision #{allow.get('decision')})", "", "Divergent files:"] + [f"- {f}" for f in files])
