"""infinievolve new|run|status|report|export|reset|flag|clear

A campaign is a directory whose config.yaml is its only configuration; everything it produces stays in
that directory."""
from __future__ import annotations

import argparse
from importlib import metadata, resources
import json
from pathlib import Path
import shutil
import sys

from .adapters import make_adapter
from .config import campaign_config
from .core.archive import CampaignStore, write_json
from .core.events import now
from .core.loop import Scheduler, campaign_state, open_campaign
from .core.types import FLAG_LEVELS
from .core.views import summary_markdown, write_matrix, write_summary
from .export import export


def version() -> str | None:
    try:
        return metadata.version("infinievolve")
    except metadata.PackageNotFoundError:
        return None


def opened(path: str):
    store = open_campaign(Path(path))
    config = campaign_config(store.root)
    adapter = make_adapter(config)
    return store, config, adapter, adapter.resolve_tasks(config.tasks)


def initialize(root: Path) -> bool:
    """Record provenance and the campaign_init event once; False if the campaign was already initialised."""
    store = CampaignStore(root)
    if store.events.last("campaign_init") is not None:
        return False
    config = campaign_config(root)
    adapter = make_adapter(config)
    tasks = adapter.resolve_tasks(config.tasks)
    write_json(root / "provenance.json", {"infinievolve": version(), "python": sys.version.split()[0],
                                          "tasks": tasks, "adapter": adapter.provenance(tasks)})
    defaults = config.defaults
    store.events.emit("campaign_init", None, name=config.name, adapter=config.adapter, tasks=tasks,
                      agent=defaults.agent, start=defaults.start, max_parallel=config.max_parallel,
                      overrides=config.overrides, evaluation=defaults.evaluation,
                      stop={"max_sessions": defaults.max_sessions, "window_k": defaults.window_k,
                            "min_improvement": defaults.min_improvement})
    write_summary(store, tasks)
    print(f"initialised {root} ({len(tasks)} task{'s' if len(tasks) != 1 else ''})")
    return True


def cmd_new(args) -> int:
    root = Path(args.campaign)
    if (root / "config.yaml").exists():
        print(f"{root}/config.yaml already exists", file=sys.stderr)
        return 1
    if args.template:
        text = Path(args.template).read_text()
    else:
        text = resources.files("infinievolve").joinpath("templates", "campaign.yaml").read_text()
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(text)
    campaign_config(root)  # validate (the name comes from the directory)
    print(f"{root}/config.yaml created; edit it, then `infinievolve run {root}`")
    return 0


def cmd_run(args) -> int:
    initialize(Path(args.campaign))
    store, config, adapter, _ = opened(args.campaign)
    state = Scheduler(store, config, adapter, log=lambda line: print(line, flush=True)).run()
    print((store.root / "summary.md").read_text(), end="")
    return 0 if not state.get("paused") else 2


def cmd_status(args) -> int:
    store, config, _, tasks = opened(args.campaign)
    print(json.dumps(campaign_state(store, config, tasks), indent=1))
    return 0


def cmd_report(args) -> int:
    store, _, _, tasks = opened(args.campaign)
    for task in tasks:
        if store.task(task).declaration() is not None:
            write_matrix(store.task(task))
    print(summary_markdown(write_summary(store, tasks)), end="")
    if args.task:
        print(f"\n## {args.task}\n")
        print((store.task(args.task).archive_dir / "matrix.md").read_text(), end="")
    return 0


def cmd_export(args) -> int:
    store, _, _, tasks = opened(args.campaign)
    print(f"exported to {export(store, tasks)}")
    return 0


def cmd_reset(args) -> int:
    """Start a task over: delete its directory and record the reset (its earlier events stop counting)."""
    store, _, _, tasks = opened(args.campaign)
    if args.task not in tasks:
        print(f"{args.task} is not a task of this campaign", file=sys.stderr)
        return 1
    root = store.task(args.task).root
    sessions = len(list((root / "sessions").glob("*/node.json"))) if root.is_dir() else 0
    if root.is_dir():
        shutil.rmtree(root)
    store.events.for_task(args.task).emit("task_reset", None, deleted_nodes=sessions, reason=args.reason)
    print(f"reset {args.task} ({sessions} archived nodes deleted)")
    return 0


def archived_node(args):
    store, _, _, tasks = opened(args.campaign)
    if args.task not in tasks:
        print(f"{args.task} is not a task of this campaign", file=sys.stderr)
        return None, None
    task_store = store.task(args.task)
    node = task_store.node(args.session)
    if node is None:
        print(f"{args.session} is not archived in {args.task}", file=sys.stderr)
    return task_store, node


def cmd_flag(args) -> int:
    """Record a person's finding about a node; a hard flag keeps it out of ranking, a void one says no
    session happened."""
    task_store, node = archived_node(args)
    if node is None:
        return 1
    node.flags[args.flag] = {"level": args.level, "note": args.reason}
    node.flags_cleared = None   # a new finding outranks an earlier clearance
    task_store.update_node(node)
    write_matrix(task_store)
    task_store.events.emit("flagged", args.session, flag=args.flag, level=args.level, reason=args.reason,
                           source="person")
    print(f"flagged {args.task}/{args.session} as {args.flag} ({args.level})")
    return 0


def cmd_clear(args) -> int:
    """Let a flagged node back into ranking after a person checked it."""
    task_store, node = archived_node(args)
    if node is None:
        return 1
    if not node.flags:
        print(f"{args.session} has no flags")
        return 0
    node.flags_cleared = {"flags": sorted(node.flags), "reason": args.reason, "at": now()}
    task_store.update_node(node)
    write_matrix(task_store)
    task_store.events.emit("flags_cleared", args.session, flags=sorted(node.flags), reason=args.reason)
    print(f"cleared {', '.join(sorted(node.flags))} on {args.task}/{args.session}; it is ranked again")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="infinievolve")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create <campaign>/config.yaml from a template")
    new.add_argument("campaign", help="campaign directory to create")
    new.add_argument("--template", help="a config.yaml to copy (default: the commented template)")
    new.set_defaults(func=cmd_new)
    for name, func, text in (("run", cmd_run, "run every task until it stops (re-attaches to running jobs)"),
                             ("status", cmd_status, "print the campaign state"),
                             ("report", cmd_report, "rewrite and print the summary (and one task's matrix)"),
                             ("export", cmd_export, "write lineage graphs and CSVs to exports/"),
                             ("reset", cmd_reset, "delete one task's sessions so it starts over"),
                             ("flag", cmd_flag, "record a finding about a node (shown to later sessions)"),
                             ("clear", cmd_clear, "rank a flagged node again after checking it by hand")):
        command = sub.add_parser(name, help=text)
        command.add_argument("campaign", help="campaign directory")
        if name == "report":
            command.add_argument("--task", help="also print this task's session matrix")
        if name == "reset":
            command.add_argument("--task", required=True)
            command.add_argument("--reason", default="")
        if name in ("flag", "clear"):
            command.add_argument("--task", required=True)
            command.add_argument("--session", required=True)
            command.add_argument("--reason", required=True, help="why; later sessions read it")
        if name == "flag":
            command.add_argument("--flag", required=True, help="a short name for the finding")
            command.add_argument("--level", choices=FLAG_LEVELS, default="hard",
                                 help="hard: not ranked (default); soft: recorded only; void: no session happened")
        command.set_defaults(func=func)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
