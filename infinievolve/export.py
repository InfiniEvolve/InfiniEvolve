"""Exports for analysis: each task's lineage as a graph, and flat tables of every node.

    exports/<task>/lineage.{json,dot,mmd}
    exports/nodes.csv          one row per node, with every declared metric of its archived version
    exports/scores.csv         one row per scored version of every node
    exports/best_so_far.csv    the best ranking value after each session
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .core.archive import CampaignStore, TaskStore, write_json
from .core.prompt import metric_value
from .core.selection import reference_node, versus
from .core.stopping import best_curve


def lineage(store: TaskStore) -> dict:
    declaration = store.declaration()
    nodes = store.nodes()
    return {"task": store.name, "rank_by": declaration.rank_by if declaration else None,
            "nodes": [{"id": node.session_id, "parent": node.parent_id,
                       "parents": node.parent_ids or ([node.parent_id] if node.parent_id else []),
                       "round": node.round_id, "variation": node.variation, "valid": node.valid,
                       "value": node.rank_value, "excluded": node.excluded,
                       "text": declaration.rank.text(node.rank_value) if declaration else "-"} for node in nodes]}


def dot(graph: dict) -> str:
    lines = ["digraph lineage {", "  rankdir=LR;", "  node [shape=box];"]
    for node in graph["nodes"]:
        style = ', style="dashed"' if not node["valid"] or node["excluded"] else ""
        lines.append(f'  {node["id"]} [label="{node["id"]}\\n{node["text"]}"{style}];')
    lines += [f'  {parent} -> {node["id"]};' for node in graph["nodes"]
              for parent in node.get("parents", [node["parent"]] if node["parent"] else [])]
    return "\n".join(lines + ["}"]) + "\n"


def mermaid(graph: dict) -> str:
    lines = ["graph LR"]
    for node in graph["nodes"]:
        lines.append(f'  {node["id"]}["{node["id"]}<br/>{node["text"]}"]')
    lines += [f'  {parent} --> {node["id"]}' for node in graph["nodes"]
              for parent in node.get("parents", [node["parent"]] if node["parent"] else [])]
    return "\n".join(lines) + "\n"


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export(store: CampaignStore, tasks: list[str]) -> Path:
    out = store.exports_dir
    node_rows, score_rows, best_rows, metric_names = [], [], [], []
    for task in tasks:
        task_store = store.task(task)
        declaration = task_store.declaration()
        if declaration is None:
            continue
        graph = lineage(task_store)
        write_json(out / task / "lineage.json", graph)
        (out / task / "lineage.dot").write_text(dot(graph))
        (out / task / "lineage.mmd").write_text(mermaid(graph))
        metric_names += [name for name in declaration.metric_names() if name not in metric_names]
        nodes = task_store.nodes()
        reference = reference_node([node for node in nodes if not node.void], declaration.rank)
        for node in nodes:
            node_rows.append({
                "reference": reference.session_id if reference else None,
                "vs_reference": versus(declaration.rank, node.rank_value, reference.rank_value)
                if reference and node.valid else None,
                "task": task, "session": node.session_id, "index": node.index, "parent": node.parent_id,
                "start": node.start_strategy, "scored": node.chosen_label, "valid": node.valid,
                "round_id": node.round_id, "parent_ids": json.dumps(node.parent_ids),
                "variation": json.dumps(node.variation, sort_keys=True),
                "rank_metric": node.rank_metric, "rank_value": node.rank_value,
                **{name: metric_value(node, name) for name in declaration.metrics},
                "flags": json.dumps({name: flag.get("level") for name, flag in node.flags.items()}, sort_keys=True),
                "flags_cleared": bool(node.flags_cleared), "void": node.void, "excluded": node.excluded,
                "artifact_kind": node.artifact.get("kind"), "artifact_fingerprint": node.artifact.get("fingerprint"),
                "agent": json.dumps(node.agent, sort_keys=True) if node.agent else "",
                "agent_seconds": node.timing.get("agent_seconds"), "wall_seconds": node.timing.get("wall_seconds"),
                "usage": json.dumps(node.usage, sort_keys=True), "error": node.error, "archived_at": node.archived_at})
            for row in node.scores:
                score_rows.append({"task": task, "session": node.session_id, "label": row.get("label"),
                                   "archived": row.get("label") == node.chosen_label,
                                   "valid": declaration.valid(row.get("checks")),
                                   **{name: (row.get("metrics") or {}).get(name) for name in declaration.metrics},
                                   "checks": json.dumps(row.get("checks") or {}, sort_keys=True),
                                   "reasons": json.dumps(row.get("reasons") or [])})
        real = [node for node in nodes if not node.void]
        curve = best_curve(real, declaration.rank)
        for place, value in enumerate(curve):
            session = "start" if place == 0 else [node for node in real if not node.is_root][place - 1].session_id
            best_rows.append({"task": task, "sessions": place, "session": session, "best": value})
    fixed = ["task", "session", "index", "parent", "start", "scored", "valid", "rank_metric", "rank_value",
             "reference", "vs_reference", "round_id", "parent_ids", "variation"]
    tail = ["flags", "flags_cleared", "void", "excluded", "artifact_kind", "artifact_fingerprint", "agent",
            "agent_seconds", "wall_seconds", "usage", "error", "archived_at"]
    write_csv(out / "nodes.csv", node_rows, fixed + metric_names + tail)
    write_csv(out / "scores.csv", score_rows,
              ["task", "session", "label", "archived", "valid", *metric_names, "checks", "reasons"])
    write_csv(out / "best_so_far.csv", best_rows, ["task", "sessions", "session", "best"])
    return out
