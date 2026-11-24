"""The design pillar's plan, review and ship rules, and `factory design add/check/expect/review/refresh` [L-9, L-122, L-186, L-187, L-188, L-190]."""
from __future__ import annotations

import json
from pathlib import Path

from . import acquire, design, human
from .plan import chash
from .run import Run
from .util import FactoryError, fwd, now_iso, read_json, safe_id, write_atomic


def load(run: Run, frame_id: str) -> tuple[dict | None, dict | None, list[str]]:
    """(frame envelope, image envelope, problems): contracts present, raws unchanged, image matching the frame [L-122]."""
    frame, probs = acquire.load(run, "design_frame", frame_id)
    if frame is None or probs:
        return frame, None, [f"design frame {frame_id!r}: " + p for p in probs] if frame else [
            f"design frame {frame_id!r} has no contract: factory ingest design_frame (agent) or factory fetch design_frame {frame_id} (adapter) [L-186]"]
    image, probs = acquire.load(run, "design_image", frame["data"]["image"])
    if image is None or probs:
        return frame, None, [f"design image {frame['data']['image']!r} of frame {frame_id!r}: " + p for p in (probs or ["contract is missing"])]
    return frame, image, []


def contract_hashes(run_dir: Path, frame_id: str) -> dict:
    """The frame and image contract hashes a `visual` or `style` spec hash covers [L-59]."""
    out: dict = {}
    try:
        f = read_json(run_dir / "contracts" / "design_frame" / f"{safe_id(frame_id)}.json")
        out = {"frame": f["raw"]["sha256"], "frame_data": chash(f["data"])}
        out["image"] = read_json(run_dir / "contracts" / "design_image" / f"{safe_id(f['data']['image'])}.json")["raw"]["sha256"]
    except (FactoryError, OSError, KeyError):
        pass
    return out


def plan_frames(plan: dict) -> list[str]:
    return sorted({f for a in plan["acs"] for f in a.get("design", [])})


def visual_inputs(run: Run, cfg: dict, check: dict) -> dict:
    """What the runner needs for a `visual` check: the design PNG, the crop box, masks and the ceilings of the frame's fidelity."""
    frame, image, problems = load(run, check["frame"])
    if problems:
        raise FactoryError("; ".join(problems))
    fd, im = frame["data"], image["data"]
    box, probs = design.crop_box(fd, check.get("node"), im)
    if probs:
        raise FactoryError("; ".join(probs))
    tol = design.tolerances(cfg, fd["fidelity"])
    return {"design": run.dir / image["raw"]["path"], "crop": box, "fidelity": fd["fidelity"], "tol": tol, "size": [im["width"], im["height"]],
            "ratio": check.get("max_diff_ratio", tol["max_diff_ratio"])}


def style_inputs(run: Run, cfg: dict, check: dict) -> dict:
    frame, _, problems = load(run, check["frame"])
    if problems:
        raise FactoryError("; ".join(problems))
    return {"fidelity": frame["data"]["fidelity"], "tol": design.tolerances(cfg, frame["data"]["fidelity"])}


def _style_map_problems(run: Run, cfg: dict, c: dict, fd: dict, here: str) -> list[str]:
    try:
        doc = read_json(run.dir / c["map"])
    except FactoryError as exc:
        return [f"{here}.map: {exc}"]
    tol, errs = design.tolerances(cfg, fd["fidelity"]), []
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not (isinstance(doc, dict) and doc.get("frame") == c["frame"] and isinstance(entries, list) and entries):
        return [f"{here}.map: must be {{\"frame\": \"{c['frame']}\", \"entries\": [{{selector, node, tolerance_px, expect}}]}} (factory design expect writes the skeleton)"]
    for i, e in enumerate(entries):
        w = f"{here}.map entries[{i}]"
        node = design.find(fd, e.get("node")) if isinstance(e, dict) and e.get("node") else None
        if not (isinstance(e, dict) and set(e) <= {"selector", "node", "tolerance_px", "expect"} and isinstance(e.get("selector"), str) and e["selector"].strip()
                and node is not None and isinstance(e.get("expect"), dict) and e["expect"]):
            errs.append(f"{w}: needs a selector, a node id of frame {c['frame']} and a non-empty expect")
            continue
        if "tolerance_px" in e and not (isinstance(e["tolerance_px"], (int, float)) and not isinstance(e["tolerance_px"], bool) and 0 <= e["tolerance_px"] <= tol["px"]):
            errs.append(f"{w}.tolerance_px: above the ceiling {tol['px']:g} for {fd['fidelity']} frames (limits in factory.toml) [L-188]")
        facts = design.facts(node)
        for prop, want in e["expect"].items():
            if prop == "text" and not str(want).strip():
                errs.append(f"{w}.expect.text: an empty text proves nothing [L-54]")
                continue
            have = facts.get(prop)
            same = prop in facts and (str(want).lower() == str(have).lower() if isinstance(have, str) else
                                      isinstance(want, (int, float)) and not isinstance(want, bool) and abs(want - have) < 1e-6)
            if not same:
                errs.append(f"{w}.expect.{prop}: {want!r} is not a fact of node {node['id']} in the frame contract"
                            + (f" (the frame says {have!r})" if prop in facts else "") + " [L-187]")
            elif prop == "text" and not str(want).strip():
                errs.append(f"{w}.expect.text: an empty text proves nothing [L-54]")
    return errs


def plan_problems(run: Run, project, st: dict, plan: dict) -> list[str]:
    """Design rules of `gate plan` [L-186, L-187, L-188, L-190]."""
    cfg, out, seen = project.cfg, [], {}
    for fid in plan_frames(plan):
        frame, image, probs = load(run, fid)
        out += probs
        if frame and image:
            seen[fid] = (frame["data"], image["data"])
    for a in plan["acs"]:
        for fid in a.get("design", []):
            mine = [c for c in plan["checks"] if a["id"] in (c.get("ac") or []) and c.get("frame") == fid]
            out += [f"{a['id']}: design frame {fid} needs at least one {t} check naming this AC and the frame [L-186]"
                    for t in ("visual", "style") if not any(c.get("type") == t for c in mine)]
    for c in plan["checks"]:
        if c.get("type") not in ("visual", "style"):
            continue
        here = f"plan.checks[{c['id']}]"
        for a in plan["acs"]:
            if a["id"] in (c.get("ac") or []) and c.get("frame") not in a.get("design", []):
                out.append(f"{here}.frame: {c.get('frame')!r} is not in the design list of {a['id']}")
        if c.get("frame") not in seen:
            continue
        fd, im = seen[c["frame"]]
        if c["type"] == "style":
            out += _style_map_problems(run, cfg, c, fd, here)
            continue
        tol = design.tolerances(cfg, fd["fidelity"])
        box, probs = design.crop_box(fd, c.get("node"), im)
        out += [f"{here}: {p}" for p in probs]
        if c.get("max_diff_ratio", 0) > tol["max_diff_ratio"]:
            out.append(f"{here}.max_diff_ratio: {c['max_diff_ratio']} is above the {fd['fidelity']} ceiling {tol['max_diff_ratio']} [L-188]")
        out += design.mask_problems(c.get("masks", []), box, here)
    links = [ln for ln in (_ticket(run, st).get("links") or []) if ln.get("rel") == "design"]
    named = [f for f in (seen.get(fid) for fid in plan_frames(plan)) if f]
    for ln in links:
        t = ln["target"]
        asked = any(q["answered"] is not None and q["ref"] == f"design:{t}" for q in st["questions"].values())
        if not asked and not any(t in (fd["id"], fd["source"]["file"]) or fd["id"] in t for fd, _ in named):
            out.append(f"ticket design link {t}: no ingested frame in the plan covers it and no answered question has --ref design:{t} [L-190]")
    if (plan.get("evidence") or {}).get("screenshots"):
        for r in plan["repos"]:
            if next((x for x in cfg["repo"] if x["name"] == r), {}).get("env") and not any(c.get("type") == "screenshot" and c.get("repo") == r for c in plan["checks"]):
                out.append(f"plan.evidence.screenshots is true, so repo {r} needs a screenshot check [L-56, L-190]")
    return out


def _ticket(run: Run, st: dict) -> dict:
    env = acquire.load(run, "ticket", st["ticket"])[0]
    return env["data"] if env else {}


def approx_frames(run: Run, plan: dict) -> list[tuple[str, str]]:
    """(frame id, frame hash) of every approx frame the plan's ACs name."""
    out = []
    for fid in plan_frames(plan):
        env = acquire.load(run, "design_frame", fid)[0]
        if env and env["data"]["fidelity"] == "approx":
            out.append((fid, design.frame_hash(env)))
    return out


def review_reasons(run: Run, st: dict, plan: dict) -> list[str]:
    """Each approx frame needs the user's design review, bound to the frame as it is now [L-186]."""
    done = {(r["frame"], r["frame_hash"]) for r in st["design_reviews"]}
    return [f"approx frame {f}: no user design review for this frame version; ask the user to look at it, then factory design review {f} --user-said \"<words>\""
            for f, h in approx_frames(run, plan) if (f, h) not in done]


def ship_reasons(run: Run, st: dict, plan: dict) -> list[str]:
    """Exact frames are re-acquired after the last plan lock; a moved version or raw needs `plan amend` and a new verify [L-9, L-122]."""
    out = []
    for fid in plan_frames(plan):
        env = acquire.load(run, "design_frame", fid)[0]
        if not env or env["data"]["fidelity"] != "exact":
            continue
        last = next((r for r in reversed(st["design_refreshes"]) if r["frame"] == fid), None)
        if last is None or last["at"] < st["lock_at"]:
            out.append(f"exact frame {fid}: run factory design refresh {fid} after the last plan lock, so ship knows the design did not move [L-9]")
        elif last["changed"]:
            out.append(f"exact frame {fid} moved at the source (version {last['version']}): factory plan amend, then verify again [L-122]")
    return out


def add(run: Run, project, png: Path, name: str, ident: str | None, actor: str | None) -> dict:
    """An approximate image becomes a design_image contract, plus the skeleton of an approx frame the agent completes [L-186]."""
    raw = Path(png).read_bytes()
    w, h = design.png_size(raw)
    ident = ident or safe_id(name.lower().replace(" ", "-"))
    env = acquire.store(run, project.cfg, "design_image", {"id": ident}, raw, ".png", "agent", None)
    est = {f"box.{k}": True for k in "xywh"}
    skeleton = {"id": ident, "name": name, "fidelity": "approx", "source": {"file": Path(png).name, "version": None, "fetched_at": now_iso()},
                "image": ident, "unknowns": [], "root": {"id": f"{ident}-root", "name": name, "type": "frame", "box": {"x": 0, "y": 0, "w": w, "h": h}, "estimated": est}}
    path = run.dir / "input" / f"design_frame-{safe_id(ident)}.json"
    write_atomic(path, json.dumps(skeleton, indent=2) + "\n")
    return {"image": env["raw"], "skeleton": fwd(path), "width": w, "height": h,
            "ingest": f'factory ingest design_frame --raw "{fwd(run.dir / env["raw"]["path"])}" --contract "{fwd(path)}"'}


def expect(run: Run, project, frame_id: str, node_id: str, selector: str, actor: str | None) -> Path:
    """Append a style-map entry holding every fact the frame states about the node; the agent prunes it, never adds [L-187]."""
    frame, _, probs = load(run, frame_id)
    if probs and frame is None:
        raise FactoryError(probs[0])
    node = design.find(frame["data"], node_id)
    if node is None:
        raise FactoryError(f"frame {frame_id} has no node {node_id!r}")
    path = run.dir / "checks" / "style" / f"{safe_id(frame_id)}.json"
    doc = read_json(path) if path.is_file() else {"frame": frame_id, "entries": []}
    facts = {k: v for k, v in design.facts(node).items() if k != "text" or str(v).strip()}
    doc["entries"].append({"selector": selector, "node": node["id"], "expect": facts})
    write_atomic(path, json.dumps(doc, indent=2) + "\n")
    return path


def review(run: Run, project, frame_id: str, said: str, actor: str | None) -> dict:
    frame, _, probs = load(run, frame_id)
    if probs:
        raise FactoryError("; ".join(probs))
    if frame["data"]["fidelity"] != "approx":
        raise FactoryError(f"frame {frame_id} is exact; only approx frames need the user's design review")
    text = human._said(said)
    return run.append({"type": "design_review", "frame": frame_id, "frame_hash": design.frame_hash(frame), "user_said": text}, actor)


def refresh(run: Run, project, frame_id: str, said: str | None, actor: str | None) -> dict:
    """Re-acquire an exact frame through its adapter (or take the user's word that it did not move) and record whether it changed [L-9]."""
    frame, image, probs = load(run, frame_id)
    if probs:
        raise FactoryError("; ".join(probs))
    fd = frame["data"]
    if fd["fidelity"] != "exact":
        raise FactoryError(f"frame {frame_id} is approx; refresh is for exact frames")
    cfg, extra = project.cfg, {}
    if cfg["providers"]["design_frame"].get("fetch"):
        new = acquire.fetch(run, cfg, project.dir, "design_frame", frame_id, tag={"refresh": True})
        img = image
        if cfg["providers"]["design_image"].get("fetch"):
            img = acquire.fetch(run, cfg, project.dir, "design_image", fd["image"], tag={"refresh": True})
        changed = (new["data"]["source"]["version"] != fd["source"]["version"] or new["raw"]["sha256"] != frame["raw"]["sha256"]
                   or img["raw"]["sha256"] != image["raw"]["sha256"])
        version, raw = new["data"]["source"]["version"], new["raw"]["sha256"]
    else:
        if not said:
            raise FactoryError(f"no [providers.design_frame] adapter re-acquires {frame_id}; ask the user whether the design moved, then "
                               f"factory design refresh {frame_id} --user-said \"<words>\"")
        changed, version, raw, extra = False, fd["source"]["version"], frame["raw"]["sha256"], {"user_said": human._said(said)}
    return run.append({"type": "design_refresh", "frame": frame_id, "version": version, "raw_sha256": raw, "changed": changed, **extra}, actor)


def check(run: Run, project, st: dict) -> list[str]:
    """Every problem of every ingested frame and its image."""
    out: list[str] = []
    for (kind, ident) in sorted(st["contracts"]):
        if kind == "design_frame":
            out += load(run, ident)[2]
    return out
