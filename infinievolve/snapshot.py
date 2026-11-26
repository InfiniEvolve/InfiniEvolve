"""A campaign as one JSON snapshot (format "infinievolve-campaign", version 1): what the watch page shows.

The page is EvolveVista's; this is InfiniEvolve's side of it, so that the page reads a campaign the way the
loop wrote it. Read from the archive only: every archived session with its parent, ranking value, validity, flags, timing
and report; sessions prepared, running or being scored again; whether each task stopped or paused.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .core.archive import CampaignStore, TaskStore, read_json
from .core.types import Node
from .core.views import current_stop

REPORT_CHARS = 6000
UNFIXED = {"name": "", "direction": "max", "scale": "absolute", "format": "{:.3f}", "anchor": "baseline", "marks": []}


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def formatted(spec: str, value) -> str:
    if number(value) is None:
        return "-"
    try:
        return spec.format(value)
    except (ValueError, IndexError, KeyError):
        return f"{value:.4g}"


def stamp(root: Path) -> str:
    """Changes whenever anything the snapshot reads changes. A few file lookups per session: cheap enough to
    take once a second."""
    root, parts = Path(root), []
    for path in [root / "events.jsonl", *sorted((root / "tasks").glob("*/task.json")),
                 *sorted((root / "tasks").glob("*/sessions/*/*.json")),
                 *sorted((root / "tasks").glob("*/sessions/*/input/spec.json")),
                 *sorted((root / "tasks").glob("*/sessions/*/confirm/r*/*.json"))]:
        try:
            st = path.stat()
        except OSError:
            continue
        parts.append(f"{path.relative_to(root)}:{st.st_size}:{st.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:20]


def metric(store: TaskStore) -> dict:
    declaration = store.declaration()
    if declaration is None:   # fixed when the baseline is archived
        return dict(UNFIXED)
    m = declaration.rank
    return {"name": m.name, "direction": m.direction, "scale": m.scale, "format": m.format, "anchor": m.anchor,
            "marks": [{"label": label, "value": value} for label, value in m.marks]}


def archived(node: Node, spec: dict, store: TaskStore, formats: dict) -> dict:
    report = node.report or ""
    remeasuring = any(not (path.parent / "sample.json").is_file()
                      for path in (store.session_dir(node.session_id) / "confirm").glob("r*/job.json"))
    return {
        "id": node.session_id, "index": node.index, "parent": node.parent_id, "start": node.start_strategy,
        "status": "remeasuring" if remeasuring else "archived", "valid": node.valid,
        "value": number(node.rank_value), "display": formatted(spec["format"], node.rank_value),
        "metrics": {k: number(v) for k, v in (node.chosen().get("metrics") or {}).items()},
        "texts": {k: formatted(formats[k], v) for k, v in (node.chosen().get("metrics") or {}).items() if k in formats},
        "flags": [{"name": name, "level": flag.get("level", "hard"), "note": flag.get("note", "")}
                  for name, flag in node.flags.items()],
        "flagsCleared": bool(node.flags_cleared), "void": node.void, "excluded": node.excluded,
        "duplicate": "duplicate" in node.flags,
        "agentSeconds": number(node.timing.get("agent_seconds")), "wallSeconds": number(node.timing.get("wall_seconds")),
        "archivedAt": node.archived_at, "error": node.error,
        "report": report if len(report) <= REPORT_CHARS else report[:REPORT_CHARS] + "\n[...]",
        "cost": number((node.usage or {}).get("cost_usd")),
    }


def pending(session: Path) -> dict | None:
    spec = read_json(session / "input" / "spec.json")
    if not isinstance(spec, dict):
        return None
    job = read_json(session / "job.json")
    return {"id": session.name, "index": spec.get("index", 0), "parent": spec.get("parent_id"),
            "start": spec.get("start_strategy"), "status": "running" if (session / "job.json").is_file() else "prepared", "valid": False,
            "value": None, "display": "-", "metrics": {}, "texts": {}, "flags": [], "flagsCleared": False, "void": False,
            "excluded": False, "duplicate": False, "agentSeconds": None, "wallSeconds": None, "archivedAt": None,
            "error": None, "report": "", "cost": None, "submittedAt": (job or {}).get("submitted_at")}


def paused(store: TaskStore) -> str | None:
    """The last pause of the task, unless work went on after it."""
    reason = None
    for event in store.events.read():
        if event["type"] == "task_paused":
            reason = (event.get("data") or {}).get("message") or "paused"
        elif event["type"] in ("selection", "archived", "baseline_scored", "rescored"):
            reason = None
    return reason


def task(store: TaskStore) -> dict:
    spec = metric(store)
    declaration = store.declaration()
    formats = {name: m.format for name, m in declaration.metrics.items()} if declaration else {}
    nodes = [archived(node, spec, store, formats) for node in store.nodes()]
    done = {node["id"] for node in nodes}
    for session in sorted((store.root / "sessions").glob("*")) if (store.root / "sessions").is_dir() else []:
        if session.name not in done:
            record = pending(session)
            if record:
                nodes.append(record)
    nodes.sort(key=lambda n: (n["index"], n["id"]))
    stop = current_stop(store)
    return {"name": store.name, "metric": spec, "nodes": nodes,
            "stopped": (stop.get("data") or {}).get("reason") if stop else None, "paused": paused(store)}


def snapshot(root) -> dict:
    root = Path(root)
    if not (root / "config.yaml").is_file():
        raise ValueError(f"{root} is not a campaign directory (no config.yaml)")
    store = CampaignStore(root)
    names = sorted(path.name for path in (root / "tasks").iterdir() if path.is_dir()) if (root / "tasks").is_dir() else []
    return {"format": "infinievolve-campaign", "version": 1, "campaign": root.resolve().name,
            "stamp": stamp(root), "tasks": [task(store.task(name)) for name in names]}


if __name__ == "__main__":
    import sys
    print(json.dumps(snapshot(sys.argv[1]), indent=1, ensure_ascii=False))
