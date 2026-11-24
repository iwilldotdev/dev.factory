"""Mirror diff gate (DESIGN section 9): a hotfix mirror carries the SAME CHANGES as the base fix.

Per code repo and mirror target, the net change of the base branch (merge-base(base target, HEAD)..HEAD of the base
worktree) is compared with the net change of the mirror branch. Commits and branches may differ; the changed lines, and
the place they sit in, may not. A recorded user decision ("mirror diff: <reason>") can allow one named divergence
(`run["mirror_allow"]`).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter

from .core import (FactoryError, code_repos, event, git, hotfix_of, now, repo_path_for_run, run_base_branch,
                    target_branch, target_worktree)

PREFIX = "mirror diff:"
EXCERPT_LINES = 6        # lines shown per hunk
EXCERPT_WIDTH = 120      # characters kept per line
EXCERPT_HUNKS = 3        # hunks shown per file and side
ERROR_FILES = 10         # files named in one gate failure
PATHSPEC_CHARS = 20000   # rename detection is limited to the changed paths while they fit one command line

# Relative import/require paths: "./x", '../y/z' in quotes; python `from .pkg import`.
_REL_QUOTED = re.compile(r"""(['"`])\.{1,2}/[^'"`\n]*\1""")
_REL_PY = re.compile(r"\bfrom\s+\.+[\w.]*\s+import\b")


def _norm_line(line: str, relaxed: bool) -> str:
    """Exact text, except (relaxed, file at another path on the other side) relative import paths are masked.
    Whitespace is never touched: a reformat, a trailing space or a CR must show."""
    if not relaxed:
        return line
    line = _REL_QUOTED.sub(lambda m: f"{m.group(1)}<rel>{m.group(1)}", line)
    return _REL_PY.sub("from <rel> import", line)


def _resolve(path, ref: str) -> str:
    from .commands import gate
    return gate.resolve_base(path, ref)


def _git(wt, *args: str) -> str:
    """git output as exact text: bytes, no strip, no newline translation (a CRLF or a trailing space must show),
    decoded utf-8 with surrogateescape so any byte survives. Callers split on "\\n" only. Local to this module: the
    shared `core.git` (universal newlines + strip) stays as it is for every other caller."""
    proc = subprocess.run(["git", "-C", str(wt), *args], capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise FactoryError(f"git {' '.join(args)} failed in {wt}: {err}")
    return proc.stdout.decode("utf-8", "surrogateescape")


def _show(text: str, show_cr: bool = False) -> str:
    """Printable text. A carriage return is shown (as \\r) only when a line-ending difference is what diverges;
    elsewhere a CRLF file would carry it on every line."""
    if show_cr:
        text = text.replace("\r", "\\r")
    elif text.endswith("\r"):
        text = text[:-1]
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


# A hunk is (function context, lines): the text git puts after the second `@@` (may be empty) and the body of a `-U1`
# diff hunk, every line with its one-character prefix: " " unchanged neighbour, "-" removed, "+" added.

def _norm_hunk(h: tuple, relaxed: bool) -> tuple:
    ctx, lines = h
    return (_norm_line(ctx, relaxed), tuple(l[:1] + _norm_line(l[1:], relaxed) for l in lines))


def _parse_patch(chunk: list[str]) -> list[tuple]:
    """Hunks of one file's patch (the lines of one `diff --git` chunk, header included)."""
    hunks = []
    ctx = None
    body: list[str] = []
    for raw in chunk:
        if raw.startswith("@@"):
            if ctx is not None and body:
                hunks.append((ctx, tuple(body)))
            m = re.match(r"@@ [^@]*@@ ?(.*)$", raw)
            ctx, body = (m.group(1) if m else ""), []
        elif ctx is None:
            continue  # file header
        elif raw.startswith("\\"):
            if body:
                body[-1] += "\\NONL"
        elif raw[:1] in (" ", "-", "+"):
            body.append(raw)
    if ctx is not None and body:
        hunks.append((ctx, tuple(body)))
    return hunks


def _parse_raw(raw: str) -> list[tuple[str, str, str, bool]]:
    """[(path, status, new blob id, same blob?)] from `git diff --raw -z --no-renames`: `:om nm osha nsha S\\0path\\0`."""
    tok = raw.split("\0")
    out = []
    i = 0
    while i + 1 < len(tok) and tok[i].startswith(":"):
        meta = tok[i][1:].split()
        out.append((tok[i + 1], meta[4], meta[3], meta[2] == meta[3]))
        i += 2
    return out


def _net_diff(wt, base_ref: str) -> tuple[str, dict]:
    """(merge-base, {path: [hunk, ...]}) for merge-base(base_ref, HEAD)..HEAD. Paths come from `--raw -z` (never quoted)
    and the patch is cut at its `diff --git` lines and matched to them in order: no path is parsed from a header."""
    mb = _git(wt, "merge-base", base_ref, "HEAD").strip()
    common = ["-c", "core.quotePath=false", "diff", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv"]
    entries = _parse_raw(_git(wt, *common, "--raw", "-z", "--no-abbrev", mb, "HEAD"))
    if not entries:
        return mb, {}
    chunks: list[list[str]] = []
    for line in _git(wt, *common, "-U1", "--src-prefix=a/", "--dst-prefix=b/", mb, "HEAD").split("\n"):
        if line.startswith("diff --git "):  # hunk bodies always start with " ", "+", "-" or "\\": never a false header
            chunks.append([])
        if chunks:
            chunks[-1].append(line)
    if len(chunks) != len(entries):
        raise FactoryError(f"git diff listed {len(entries)} file(s) but {len(chunks)} patch(es) in {wt}")
    files: dict[str, list] = {}
    for (path, status, new_sha, same), chunk in zip(entries, chunks):
        hunks = _parse_patch(chunk)
        if not hunks:
            if same:
                continue  # mode-only change: ignored
            hunks = [("", (f"+<no text hunks: binary, empty or submodule; {status} {new_sha}>",))]
        files[path] = hunks
    return mb, files


def _renames(wt, base_ref: str, mirror_ref: str, paths: set[str]) -> dict[str, str]:
    """{path on the mirror target: path on the base target} for files moved between the two target branches. Detection
    is limited to `paths` (the files of the two net diffs): a rename needs its old and its new path in it, and both are,
    because the base fix touches the old path and the mirror the new one. Too many paths for a command line: whole tree."""
    if not paths:
        return {}
    spec = sorted(paths)
    if sum(len(p) + 1 for p in spec) > PATHSPEC_CHARS:
        spec = []
    out = _git(wt, "--literal-pathspecs", "-c", "core.quotePath=false", "-c", "diff.renameLimit=10000", "diff",
               "--name-status", "-M", "--diff-filter=R", "-z", base_ref, mirror_ref, "--", *spec)
    tok = out.split("\0")
    moved = {}
    for i in range(0, len(tok) - 2, 3):  # -z: status, old, new
        if tok[i].startswith("R"):
            moved[tok[i + 2]] = tok[i + 1]
    return moved


def _changed(h: tuple) -> list[str]:
    return [l for l in h[1] if l[:1] in ("+", "-")]


def _excerpt(hunks: list, show_cr: bool = False) -> list[str]:
    """Capped text of the first hunks. A hunk longer than the cap shows only its changed lines (the neighbours would
    push the change out of view); a short one shows its neighbours too, since they are what can differ."""
    out = []
    for h in hunks[:EXCERPT_HUNKS]:
        body = list(h[1]) if len(h[1]) <= EXCERPT_LINES else _changed(h)
        lines = ([f"@@ {h[0]}"] if h[0] else []) + body
        shown = [_show(l if len(l) <= EXCERPT_WIDTH else l[:EXCERPT_WIDTH] + "...", show_cr) for l in lines[:EXCERPT_LINES]]
        if len(lines) > EXCERPT_LINES:
            shown.append(f"... (+{len(lines) - EXCERPT_LINES} more line(s))")
        out.append("\n".join(shown))
    if len(hunks) > EXCERPT_HUNKS:
        out.append(f"... (+{len(hunks) - EXCERPT_HUNKS} more hunk(s))")
    return out


def _delta(f: dict, side: str) -> list[str]:
    """Changed lines present on `side` ("base_only"/"mirror_only") and missing on the other one."""
    other = "mirror_only" if side == "base_only" else "base_only"
    mine = Counter(l for h in f[side] for l in _changed(h))
    theirs = Counter(l for h in f[other] for l in _changed(h))
    return list((mine - theirs).elements())


def _no_cr(h: tuple) -> tuple:
    return (h[0].rstrip(chr(13)), tuple(l.replace(chr(13), "") for l in h[1]))


def _grouped(hunks: list) -> dict[str, Counter]:
    """Changed lines (not neighbours) per function context."""
    out: dict[str, Counter] = {}
    for h in hunks:
        out.setdefault(h[0], Counter()).update(_changed(h))
    return out


def compare_files(base: dict, mirror: dict, moved: dict[str, str]) -> list[dict]:
    """Divergent files between two parsed net diffs. `moved` maps mirror-target paths to base-target paths.

    Hunk key = (git function context, -U1 hunk body: the changed lines plus their immediate unchanged neighbours), every
    line exact. The exact keys must match as a multiset. Hunks left over (diff grouped the same lines differently) still
    match when, inside each NON-EMPTY function context, their changed lines are the same multiset; a leftover hunk with
    no function context never matches that way, so the same text placed elsewhere in the file is a divergence."""
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
        bh = [_norm_hunk(h, relaxed) for h in base[key]]
        mh = [_norm_hunk(h, relaxed) for h in mh]
        cb, cm = Counter(bh), Counter(mh)
        if cb == cm:
            continue
        left_b, left_m = list((cb - cm).elements()), list((cm - cb).elements())
        if all(h[0] for h in left_b + left_m) and _grouped(left_b) == _grouped(left_m):
            continue
        same_lines = Counter(l for h in bh for l in _changed(h)) == Counter(l for h in mh for l in _changed(h))
        eol_only = Counter(_no_cr(h) for h in bh) == Counter(_no_cr(h) for h in mh)
        out.append({"file": key, "mirror_file": mpath if relaxed else None, "kind": "hunks", "same_lines": same_lines,
                    "eol_only": eol_only,
                    "base_only": left_b, "mirror_only": left_m})
    return out


def _ref_info(wt, branch: str, used: str, merge_base: str | None) -> dict:
    def rp(ref: str) -> str:
        return git(wt, "rev-parse", "--verify", "--quiet", ref, check=False)
    local, origin = rp(f"refs/heads/{branch}"), rp(f"refs/remotes/origin/{branch}")
    return {"branch": branch, "ref": used, "tip": rp(used), "merge_base": merge_base,
            "stale_hint": bool(local and origin and local != origin)}


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
                bname, mname = run_base_branch(run, ws, alias), target_branch(ws, alias, target)
                base_ref, mirror_ref = _resolve(base_wt, bname), _resolve(mirror_wt, mname)
                entry["refs"] = {"base": _ref_info(base_wt, bname, base_ref, None),
                                 "mirror": _ref_info(mirror_wt, mname, mirror_ref, None)}
                base_mb, base = _net_diff(base_wt, base_ref)
                mirror_mb, mirror = _net_diff(mirror_wt, mirror_ref)
                entry["refs"]["base"]["merge_base"], entry["refs"]["mirror"]["merge_base"] = base_mb, mirror_mb
                moved = _renames(base_wt, base_ref, mirror_ref, set(base) | set(mirror))
                entry["files"] = compare_files(base, mirror, moved)
            except FactoryError as err:
                entry["error"] = str(err)
            entries.append(entry)
    if not entries:
        return None
    digest_src = [[e["repo"], e["target"], e.get("error"),
                   [[f["file"], f["kind"], sorted(map(list, f["base_only"])), sorted(map(list, f["mirror_only"]))]
                    for f in e["files"]]] for e in entries]
    digest = hashlib.sha256(json.dumps(digest_src, sort_keys=True, ensure_ascii=False).encode("utf-8", "surrogateescape")
                            ).hexdigest()
    divergent = any(e["files"] or e.get("error") for e in entries)
    return {"equal": not divergent, "digest": digest, "entries": entries}


def public(report: dict) -> dict:
    """JSON-safe report: hunks become capped excerpts."""
    entries = []
    for e in report["entries"]:
        files = [{"file": f["file"], "mirror_file": f["mirror_file"], "kind": f["kind"],
                  "same_lines": f.get("same_lines", False),
                  "eol_only": f.get("eol_only", False),
                  "base_only": _excerpt(f["base_only"], f.get("eol_only", False)),
                  "mirror_only": _excerpt(f["mirror_only"], f.get("eol_only", False)),
                  "base_only_hunks": len(f["base_only"]), "mirror_only_hunks": len(f["mirror_only"])} for f in e["files"]]
        entries.append({"repo": e["repo"], "target": e["target"], "files": files, "refs": e.get("refs"),
                        **({"error": e["error"]} if e.get("error") else {})})
    return {"equal": report["equal"], "digest": report["digest"], "entries": entries}


def _file_line(f: dict) -> str:
    where = f["file"] + (f" (mirror path {f['mirror_file']})" if f.get("mirror_file") and f["mirror_file"] != f["file"] else "")
    if f["kind"] == "only_base":
        return f"{where}: changed only on the base branch (missing on the mirror)"
    if f["kind"] == "only_mirror":
        return f"{where}: changed only on the mirror branch (not in the base fix)"
    line = f"{where}: {f['base_only_hunks']} hunk(s) only on base, {f['mirror_only_hunks']} only on mirror"
    if f.get("eol_only"):
        line += " (only the line endings differ: CRLF vs LF)"
    elif f.get("same_lines"):
        line += " (same lines, different place/context: the surrounding code differs on the mirror target)"
    return line


def _refs_line(e: dict) -> str | None:
    refs = e.get("refs")
    if not refs:
        return None
    def one(r: dict) -> str:
        return f"{r['ref']}@{(r['tip'] or '?')[:9]} merge-base {(r['merge_base'] or '?')[:9]}"
    line = f"refs {e['repo']}/{e['target']}: compared base {one(refs['base'])}; mirror {one(refs['mirror'])}"
    if refs["base"]["stale_hint"] or refs["mirror"]["stale_hint"]:
        line += " (local and origin target refs differ: `git fetch` in the worktree if origin moved, then compare again)"
    return line


def render_refs(report: dict) -> list[str]:
    return [l for l in (_refs_line(e) for e in report["entries"]) if l]


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
    return lines + render_refs(report)


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


def _comparison_errors(report: dict) -> list[str]:
    return [f"{e['repo']}/{e['target']}: {e['error']}" for e in report["entries"] if e.get("error")]


def record_allowance(run: dict, ws: dict, n) -> None:
    """Store, on the run, the user decision #n as the allowance of the CURRENT divergence (mutates `run`; the caller saves)."""
    d = _decision(run, n)
    report = compute(run, ws)
    if report is None or report["equal"]:
        raise FactoryError("no mirror divergence to allow (the mirror diff is equal or no mirror exists yet)")
    failed = _comparison_errors(report)
    if failed:
        raise FactoryError("the mirror diff could not be computed, so there is nothing the user could allow: "
                           + "; ".join(failed))
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
    if _comparison_errors(report):
        return False  # nothing was compared: no allowance covers that
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
                cr = bool(f.get("eol_only"))
                lines = [_show(l, cr) for l in _delta(f, key)[:3]]
                if not lines and f[key]:  # same lines in another place: show the hunk with its neighbours
                    lines = _excerpt(f[key][:1], cr)[0].split("\n")[:4]
                if lines:
                    ex += f" [{side}: " + " / ".join(lines) + "]"
            out.append(f"{label}: mirror diff: {_file_line({**f, 'base_only_hunks': len(f['base_only']), 'mirror_only_hunks': len(f['mirror_only'])})}{ex}")
    total = sum(len(e["files"]) for e in report["entries"])
    if total > shown:
        out.append(f"... {total - shown} more divergent file(s); `factory run mirror-diff <run>` lists all")
    out.extend(render_refs(report))
    allow = run.get("mirror_allow")
    stale = " (the allowance recorded earlier no longer matches this divergence)" if isinstance(allow, dict) else ""
    if _comparison_errors(report):
        out.append("the mirror diff could not be compared: no user exception covers that; fix the error above (the "
                   "allowance applies only to a divergence that was actually compared)")
        return out
    out.append("the mirror must carry the same changes, in the same place, as the base fix (adapt only moved files/import paths; "
               "no reformatting, no relocated tests): fix the mirror branch, or, when the mirror target legitimately differs "
               "around the change, with the user's explicit decision `factory run decide <run> "
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
