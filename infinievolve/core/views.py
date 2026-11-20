"""Tables rebuilt from the archive: one task's session matrix and the campaign summary."""
from __future__ import annotations

from .archive import CampaignStore, TaskStore, write_json
from .prompt import failures, metric_value, minutes
from .selection import best_node, reference_node, versus, versus_text
from .stopping import best_curve


NODE_CHANGES = ("archived", "flagged", "flags_cleared", "rescored")


def current_stop(store: TaskStore) -> dict | None:
    """The task's last stop decision, unless its nodes changed after it (a new session, a flag, a repeat
    scoring), which reopens the question."""
    events = store.events.read()
    position = next((i for i in range(len(events) - 1, -1, -1) if events[i]["type"] == "task_stop"), None)
    if position is None or any(event["type"] in NODE_CHANGES for event in events[position + 1:]):
        return None
    return events[position]


def matrix_rows(store: TaskStore) -> list[dict]:
    declaration = store.declaration()
    if declaration is None:
        return []
    nodes = store.nodes()
    curve = best_curve([node for node in nodes if not node.void], declaration.rank)
    reference = reference_node([node for node in nodes if not node.void], declaration.rank)
    position = {node.session_id: place for place, node in
                enumerate((node for node in nodes if not node.void and not node.is_root), start=1)}
    rows = []
    for node in nodes:
        place = 0 if node.is_root else position.get(node.session_id)
        rows.append({
            "session": node.session_id, "parent": node.parent_id, "start": node.start_strategy,
            "scored": node.chosen_label, "valid": node.valid,
            "metrics": {name: metric_value(node, name) for name in declaration.metrics},
            "rank_value": node.rank_value, "best_so_far": curve[place] if place is not None else None,
            "reference": reference.session_id if reference else None,
            "vs_reference": versus(declaration.rank, node.rank_value, reference.rank_value) if reference else None,
            "vs_reference_text": versus_text(declaration.rank, node.rank_value, reference.rank_value)
            if reference and node.valid else "-",
            "flags": {name: flag.get("level") for name, flag in node.flags.items()},
            "flags_cleared": bool(node.flags_cleared), "failed": failures(node, declaration),
            "agent_seconds": node.timing.get("agent_seconds"), "wall_seconds": node.timing.get("wall_seconds"),
            "error": node.error})
    return rows


def matrix_markdown(store: TaskStore, rows: list[dict]) -> str:
    declaration = store.declaration()
    if declaration is None:
        return "(the baseline is not archived yet)\n"
    names = declaration.metric_names()
    header = ["session", "parent", "start", "scored", "valid", *names, "vs reference", "best so far", "flags",
              "agent min", "wall min", "failed"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        flags = ", ".join(f"{name} ({level})" for name, level in row["flags"].items())
        if flags and row["flags_cleared"]:
            flags += " [cleared]"
        cells = [row["session"], row["parent"] or "-", row["start"], row["scored"] or "-",
                 "yes" if row["valid"] else "no",
                 *[declaration.metrics[name].text(row["metrics"].get(name)) for name in names],
                 row["vs_reference_text"], declaration.rank.text(row["best_so_far"]), flags or "-",
                 minutes(row["agent_seconds"], 1),
                 minutes(row["wall_seconds"], 1), row["failed"] or "-"]
        lines.append("| " + " | ".join(str(cell) for cell in cells) + " |")
    reference = next((row["reference"] for row in rows if row["reference"]), None)
    lines += ["", f"Ranked by `{declaration.rank_by}` ({declaration.rank.direction}). " +
              (f"Reference: {reference}" + ("" if reference == "s000" else ", the first valid session (the baseline "
               "does not rank)") + "." if reference else "No reference yet: nothing ranks.")]
    return "\n".join(lines) + "\n"


def write_matrix(store: TaskStore) -> list[dict]:
    rows = matrix_rows(store)
    write_json(store.archive_dir / "matrix.json", rows)
    (store.archive_dir / "matrix.md").write_text(matrix_markdown(store, rows))
    return rows


def summary_rows(store: CampaignStore, tasks: list[str]) -> list[dict]:
    rows = []
    for task in tasks:
        task_store = store.task(task)
        declaration = task_store.declaration()
        nodes = [node for node in task_store.nodes() if not node.void]
        sessions = [node for node in nodes if not node.is_root]
        root = next((node for node in nodes if node.is_root), None)
        best = best_node(nodes, declaration.rank) if declaration else None
        reference = reference_node(nodes, declaration.rank) if declaration else None
        stop = current_stop(task_store)
        rows.append({
            "task": task, "rank_by": declaration.rank_by if declaration else None, "sessions": len(sessions),
            "valid_sessions": sum(node.valid for node in sessions),
            "baseline": root.rank_value if root and root.valid else None,
            "reference": reference.session_id if reference else None,
            "reference_text": (f"{reference.session_id} {declaration.rank.text(reference.rank_value)}"
                               if reference else "-"),
            "best_vs_reference": versus_text(declaration.rank, best.rank_value, reference.rank_value)
            if best and reference else "-",
            "best": best.rank_value if best else None, "best_session": best.session_id if best else None,
            "best_text": declaration.rank.text(best.rank_value) if best else "-",
            "baseline_text": declaration.rank.text(root.rank_value) if root and root.valid else "-",
            "stop_reason": stop["data"].get("reason") if stop else None,
            "agent_seconds": sum(node.timing.get("agent_seconds") or 0 for node in nodes),
            "wall_seconds": sum(node.timing.get("wall_seconds") or 0 for node in nodes)})
    return rows


def summary_markdown(rows: list[dict]) -> str:
    lines = ["| task | ranked by | sessions | valid | reference | best | vs reference | best session | agent h | status |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['task']} | {row['rank_by'] or '-'} | {row['sessions']} | {row['valid_sessions']} "
                     f"| {row['reference_text']} | {row['best_text']} | {row['best_vs_reference']} "
                     f"| {row['best_session'] or '-'} "
                     f"| {row['agent_seconds'] / 3600:.2f} | {row['stop_reason'] or 'open'} |")
    return "\n".join(lines) + "\n"


def write_summary(store: CampaignStore, tasks: list[str]) -> list[dict]:
    rows = summary_rows(store, tasks)
    write_json(store.root / "summary.json", rows)
    (store.root / "summary.md").write_text(summary_markdown(rows))
    return rows
