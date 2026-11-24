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

# Import statements, across languages. A line is import-like when its statement is an import: `import ...`, `from "x"` /
# `} from "x";` continuation lines, `export ... from "x"`, `require("x")`, `import("x")`, `#include "x"`, python
# `from x import` / `import x`. C# `using X;` has no path in it: not matched.
_IMPORT_LINE = (re.compile(r"^\s*(?:import|from)\b"),
                re.compile(r"""^\s*\}?\s*from\s+['"`]"""),
                re.compile(r"""^\s*export\b.*\bfrom\s+['"`]"""),
                re.compile(r"^\s*#\s*(?:include|import)\b"),
                re.compile(r"""\b(?:require|require_once|include|import)\s*\(\s*['"`]"""))
# The module specifier of an import line: quoted after from/import/require(/include, or python dotted module.
_SPEC_QUOTED = re.compile(r"""(\b(?:from|import|require(?:_once)?|include)\s*\(?\s*)(['"`])([^'"`\n]+)\2""")
_SPEC_PY_FROM = re.compile(r"^(\s*from\s+)([\w.]*\w)(\s+import\b)")
_SPEC_PY_IMPORT = re.compile(r"^(\s*import\s+)([\w.]*\w)(\s+as\s+\w+)?\s*$")
_IMPORT_ENTRY = re.compile(r"^\s*(?:[\w$]+(?:\s+as\s+[\w$]+)?\s*,?|[{}(),]*)\s*$")  # a name inside an import list


def _is_import(text: str) -> bool:
    text = text.rstrip(chr(13))
    return any(r.search(text) for r in _IMPORT_LINE)


def _stems(spec: str) -> set[str]:
    """Names a module specifier can resolve to: its last path/dotted segment, with and without its last extension."""
    last = spec.rstrip("/").split("/")[-1]
    out = {last}
    if "." in last.strip("."):
        out.add(last.rsplit(".", 1)[0])
    return {x for x in out if x and not set(x) <= {"."}}


def _specifiers(line: str) -> list[str]:
    """Module specifiers named by an import-like line (empty for any other line)."""
    if not _is_import(line):
        return []
    out = [m.group(3) for m in _SPEC_QUOTED.finditer(line)]
    for rx in (_SPEC_PY_FROM, _SPEC_PY_IMPORT):
        m = rx.match(line)
        if m and not out:
            out.append(m.group(2).replace(".", "/"))
    return out


def _mask_moved(line: str, moved_stems: frozenset) -> str:
    """Import line whose module is a file moved between the two target branches: the specifier becomes `<moved:stem>`."""
    if not moved_stems or not _is_import(line):
        return line

    def pick(spec: str) -> str | None:
        hit = sorted(_stems(spec) & moved_stems)
        return hit[0] if hit else None

    def quoted(m):
        st = pick(m.group(3))
        return m.group(0) if st is None else f"{m.group(1)}{m.group(2)}<moved:{st}>{m.group(2)}"
    out = _SPEC_QUOTED.sub(quoted, line)
    if out != line:
        return out
    for rx, g in ((_SPEC_PY_FROM, 3), (_SPEC_PY_IMPORT, 3)):
        m = rx.match(line)
        if m:
            st = pick(m.group(2).replace(".", "/"))
            if st is not None:
                return line[:m.start(2)] + f"<moved:{st}>" + line[m.end(2):]
    return line


def _norm_line(line: str, relaxed: bool, moved_stems: frozenset = frozenset()) -> str:
    """Exact text, except import module specifiers: one naming a file moved between the two target branches is masked
    everywhere, and (relaxed, file at another path on the other side) relative import paths are masked. Whitespace is
    never touched: a reformat, a trailing space or a CR must show."""
    line = _mask_moved(line, moved_stems)
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


# A hunk is (function context, lines): the text git puts after the second `@@` (may be empty) and the body of a `-U0`
# diff hunk (changed lines only; no neighbours, so the grouping does not depend on the lines around), every line with
# its one-character prefix: "-" removed, "+" added.

def _norm_hunk(h: tuple, relaxed: bool, moved_stems: frozenset = frozenset()) -> tuple:
    ctx, lines = h
    return (_norm_line(ctx, relaxed, moved_stems),
            tuple(l[:1] + _norm_line(l[1:], relaxed, moved_stems) for l in lines))


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
    for line in _git(wt, *common, "-U0", "--src-prefix=a/", "--dst-prefix=b/", mb, "HEAD").split("\n"):
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


def _rename_map(wt, base_ref: str, mirror_ref: str, spec: list[str], literal: bool) -> dict[str, str]:
    out = _git(wt, *(["--literal-pathspecs"] if literal else []), "-c", "core.quotePath=false", "-c",
               "diff.renameLimit=10000", "diff", "--name-status", "-M", "--diff-filter=R", "-z", base_ref, mirror_ref,
               "--", *spec)
    tok = out.split("\0")
    moved = {}
    for i in range(0, len(tok) - 2, 3):  # -z: status, old, new
        if tok[i].startswith("R"):
            moved[tok[i + 2]] = tok[i + 1]
    return moved


def _renames(wt, base_ref: str, mirror_ref: str, paths: set[str], stems: set[str] = frozenset()) -> dict[str, str]:
    """{path on the mirror target: path on the base target} for files moved between the two target branches. Two
    detections, merged: the files of the two net diffs (a rename needs its old and its new path in the limit, and both are
    when the base fix touches the old path and the mirror the new one), and the modules named by the import lines of the
    two net diffs (`stems`): their files are usually in neither net diff, so each stem is a pathspec `**/<stem>.*` (old
    and new path share the name). Too many paths for a command line: whole tree."""
    moved: dict[str, str] = {}
    if paths:
        spec = sorted(paths)
        if sum(len(p) + 1 for p in spec) > PATHSPEC_CHARS:
            spec = []
        moved.update(_rename_map(wt, base_ref, mirror_ref, spec, True))
    safe = sorted(x for x in stems if re.fullmatch(r"[\w.$@-]+", x))
    if safe:
        spec = [f":(glob)**/{x}{suffix}" for x in safe for suffix in (".*", "")]
        if sum(len(p) + 1 for p in spec) > PATHSPEC_CHARS:
            spec = []
        moved.update(_rename_map(wt, base_ref, mirror_ref, spec, False))
    return moved


def _moved_stems(moved: dict[str, str]) -> frozenset:
    """Module names (file name without its last extension, and without any extension; a directory's name for an
    `index` file) of the files moved between the two target branches, old and new path alike."""
    out = set()
    for new, old in moved.items():
        for path in (new, old):
            parts = path.split("/")
            name = parts[-1]
            if len(parts) > 1 and name.rsplit(".", 1)[0] == "index":
                out.add(parts[-2])
            out.add(name.rsplit(".", 1)[0] if "." in name.strip(".") else name)
            out.add(name.split(".")[0])
    return frozenset(x for x in out if x)


def _import_stems(*nets: dict) -> set[str]:
    """Module names named by the import-like changed lines of the net diffs."""
    out: set[str] = set()
    for files in nets:
        for hunks in files.values():
            for h in hunks:
                for l in _changed(h):
                    for spec in _specifiers(l[1:]):
                        out |= _stems(spec)
    return out


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


def _import_only(h: tuple) -> bool:
    """A hunk whose changed lines are only import statements (and blank lines): its placement is not checked, since for
    top-of-file imports git's function context is an arbitrary previous line. A name added inside a multi-line import
    list counts when the context line is an import statement."""
    body = [l[1:] for l in _changed(h)]
    if not any(l.strip(" \t\r") for l in body):
        return False
    ok = lambda l: not l.strip(" \t\r") or _is_import(l)
    if all(ok(l) for l in body):
        return True
    return _is_import(h[0]) and all(ok(l) or _IMPORT_ENTRY.match(l.rstrip(chr(13))) for l in body)


def _match_hunks(bh: list, mh: list) -> tuple[list, list]:
    """Hunks left over after pairing the ones with the same changed lines whose placement agrees: same function context,
    or no function context on either side, or an import-only hunk. Returns (base leftovers, mirror leftovers)."""
    def free(h):
        return not h[0] or _import_only(h)
    by_b: dict[tuple, list] = {}
    by_m: dict[tuple, list] = {}
    for h in bh:
        by_b.setdefault(tuple(_changed(h)), []).append(h)
    for h in mh:
        by_m.setdefault(tuple(_changed(h)), []).append(h)
    left_b, left_m = [], []
    for key in list(by_b) + [k for k in by_m if k not in by_b]:
        rb, rm = list(by_b.get(key, [])), list(by_m.get(key, []))
        for h in list(rb):  # same context first
            hit = next((m for m in rm if m[0] == h[0]), None)
            if hit is not None:
                rb.remove(h)
                rm.remove(hit)
        for h in list(rb):  # then a side with no context, or imports
            hit = next((m for m in rm if free(h) or free(m)), None)
            if hit is not None:
                rb.remove(h)
                rm.remove(hit)
        left_b += rb
        left_m += rm
    return left_b, left_m


def _regroup(left_b: list, left_m: list) -> bool:
    """True when the leftovers are the same changed lines that git grouped differently on the two sides: inside each
    non-empty function context, as a multiset; import-only hunks pooled whatever their context; and, when a side has a
    hunk with no function context, whatever remains pooled (the place cannot be known then)."""
    def lines(hs):
        return Counter(l for h in hs for l in _changed(h))
    imp_b, imp_m = [h for h in left_b if _import_only(h)], [h for h in left_m if _import_only(h)]
    if lines(imp_b) == lines(imp_m):
        left_b, left_m = [h for h in left_b if h not in imp_b], [h for h in left_m if h not in imp_m]
    for c in {h[0] for h in left_b + left_m if h[0]}:
        cb, cm = [h for h in left_b if h[0] == c], [h for h in left_m if h[0] == c]
        if lines(cb) == lines(cm):
            left_b, left_m = [h for h in left_b if h[0] != c], [h for h in left_m if h[0] != c]
    if not left_b and not left_m:
        return True
    return any(not h[0] for h in left_b + left_m) and lines(left_b) == lines(left_m)


def compare_files(base: dict, mirror: dict, moved: dict[str, str], moved_stems: frozenset = frozenset()) -> list[dict]:
    """Divergent files between two parsed net diffs. `moved` maps mirror-target paths to base-target paths; `moved_stems`
    names the modules moved between the two target branches (see `_moved_stems`).

    The changed lines decide: per file they must be the same after normalisation (whitespace never ignored; an import
    specifier naming a moved module masked; relative import paths masked in a file at another path). Placement is the git
    function context of each hunk, not its neighbours: the same changed lines in a different non-empty context diverge
    ("same lines, different place"), except in import-only hunks and when either side has no context (then the changed
    lines alone count). Hunks are `-U0`, so the grouping never depends on the surrounding code."""
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
        bh = [_norm_hunk(h, relaxed, moved_stems) for h in base[key]]
        mh = [_norm_hunk(h, relaxed, moved_stems) for h in mh]
        left_b, left_m = _match_hunks(bh, mh)
        if (not left_b and not left_m) or _regroup(left_b, left_m):
            continue
        same_lines = Counter(l for h in bh for l in _changed(h)) == Counter(l for h in mh for l in _changed(h))
        eol_only = Counter(_no_cr(h) for h in bh) == Counter(_no_cr(h) for h in mh)
        out.append({"file": key, "mirror_file": mpath if relaxed else None, "kind": "hunks", "same_lines": same_lines,
                    "eol_only": eol_only, "base_ctx": sorted({h[0] for h in left_b if h[0]}),
                    "mirror_ctx": sorted({h[0] for h in left_m if h[0]}),
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
                moved = _renames(base_wt, base_ref, mirror_ref, set(base) | set(mirror), _import_stems(base, mirror))
                entry["files"] = compare_files(base, mirror, moved, _moved_stems(moved))
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
                  "base_ctx": f.get("base_ctx", []), "mirror_ctx": f.get("mirror_ctx", []),
                  "base_only": _excerpt(f["base_only"], f.get("eol_only", False)),
                  "mirror_only": _excerpt(f["mirror_only"], f.get("eol_only", False)),
                  "base_only_hunks": len(f["base_only"]), "mirror_only_hunks": len(f["mirror_only"])} for f in e["files"]]
        entries.append({"repo": e["repo"], "target": e["target"], "files": files, "refs": e.get("refs"),
                        **({"error": e["error"]} if e.get("error") else {})})
    return {"equal": report["equal"], "digest": report["digest"], "entries": entries}


def _ctxs(ctxs) -> str:
    return ", ".join(f"`{_show(c)[:EXCERPT_WIDTH]}`" for c in ctxs[:3]) if ctxs else "no function context"


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
        line += (" (same lines, different place/context: base in " + _ctxs(f.get("base_ctx")) + ", mirror in "
                 + _ctxs(f.get("mirror_ctx")) + ")")
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
