"""infinievolve init|new|check|run|status|report|export|reset|flag|clear

Two ways to say what to run:

    a task directory (task.yaml, workspace/, an evaluator), made with `infinievolve init`:
        infinievolve run mytask --agent claude/<model> --sessions 6
      Each run of it is a campaign of its own under mytask/runs/<name>/ ("default" unless --name says).

    a campaign directory, whose config.yaml is its only configuration, made with `infinievolve new`:
        infinievolve run campaigns/first
      for several tasks at once, or an adapter of your own.

Either way, everything a campaign produces stays in its directory."""
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


TASK_YAML = """\
# What the agent is asked to do.
instruction: |
  Describe the task here.

# How one version is scored: run in a clean copy of the work, it prints one JSON line (see evaluate.py).
evaluate: python3 "$EVOLVE_TASK/evaluate.py"

# The number that ranks sessions.
metric: {name: score, direction: max}

# The agent, unless `infinievolve run --agent ...` names one:
#   claude/<model>[:<effort>]   codex/<model>[:<effort>]   {command: "<shell command>"}
# agent: claude/<model>
"""

EVALUATE_PY = """\
\"\"\"Scores one version of the work. Run with a clean copy of it as the current directory.\"\"\"
import json

checks = {"ok": True}        # a version counts only when every check is true
metrics = {"score": 0.0}     # TODO: measure the version in the current directory
print(json.dumps({"checks": checks, "metrics": metrics, "reasons": []}))
"""


def campaign_dir(path: str, name: str | None = None) -> Path:
    """The campaign a path means: the directory itself, or a task directory's run of that name."""
    root = Path(path)
    if (root / "config.yaml").is_file():
        return root
    if (root / "task.yaml").is_file():
        return root / "runs" / (name or "default")
    raise FileNotFoundError(f"{root} is neither a campaign (no config.yaml) nor a task directory (no task.yaml)")


def opened(path: str, name: str | None = None):
    store = open_campaign(campaign_dir(path, name))
    config = campaign_config(store.root)
    adapter = make_adapter(config)
    return store, config, adapter, adapter.resolve_tasks(config.tasks)


def cmd_init(args) -> int:
    root = Path(args.task)
    if (root / "task.yaml").exists():
        print(f"{root}/task.yaml already exists", file=sys.stderr)
        return 1
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    (root / "task.yaml").write_text(TASK_YAML)
    if not (root / "evaluate.py").exists():
        (root / "evaluate.py").write_text(EVALUATE_PY)
    print(f"{root}/ created. Put the starting files in {root}/workspace/, say what to do in {root}/task.yaml, "
          f"measure a version in {root}/evaluate.py, then:\n  infinievolve run {root} --agent claude/<model>")
    return 0


RUN_FLAGS = ("agent", "agent_command", "sessions", "start")


def first_run_config(args) -> dict:
    """The config of a task directory's run, from the command line: what its config.yaml will hold."""
    agent = {"command": args.agent_command} if args.agent_command else args.agent
    return {"campaign": {"adapter": "local", "tasks": [Path(args.campaign).resolve().name], "max_parallel": 1,
                         "poll_seconds": 1},
            **({"agent": agent} if agent else {}),
            "strategy": {"start": args.start or "best"},
            "stop": {"max_sessions": args.sessions or 10, "window_k": 3, "min_improvement": 0},
            "options": {"task_dir": "../.."}}


def prepare_run(args) -> Path:
    """For a task directory: the run's campaign directory, with a config.yaml written on its first run."""
    root = campaign_dir(args.campaign, args.name)
    chosen = [key for key in RUN_FLAGS if getattr(args, key) is not None]
    if (root / "config.yaml").is_file():
        if chosen:
            flags = ", ".join("--" + key.replace("_", "-") for key in chosen)
            raise SystemExit(f"{root} already has a config.yaml, so {flags} would be ignored: edit that file, "
                             "or start another run with --name")
        return root
    root.mkdir(parents=True, exist_ok=True)
    import yaml
    (root / "config.yaml").write_text(f"# Written by `infinievolve run {args.campaign}`. Edit it to change this run.\n"
                                      + yaml.safe_dump(first_run_config(args), sort_keys=False))
    return root


def cmd_check(args) -> int:
    """Say what a run would do, without starting anything: the tasks, how each is ranked and stopped, and,
    with --prompt, the prompt its first session would get."""
    from .config import parse_config
    from .core import prompt as prompts
    from .core.schema import parse_declaration
    root = campaign_dir(args.campaign, args.name).resolve()
    if (root / "config.yaml").is_file():
        config = campaign_config(root)
    else:   # a task directory that has not run yet: what its first run would be
        config = parse_config(first_run_config(args), name=root.name, root=root)
    adapter = make_adapter(config)
    tasks = adapter.resolve_tasks(config.tasks)
    print(f"{root}\nadapter {config.adapter}; {len(tasks)} task(s); at most {config.max_parallel} job(s) at once")
    for task in tasks:
        settings = config.for_task(task)
        declaration = parse_declaration(adapter.declaration(task)).merged(settings.evaluation)
        metric = declaration.rank
        limit = settings.session_minutes * 60 if settings.session_minutes else adapter.default_time_limit_seconds(task)
        gate = "every check the evaluator reports" if declaration.gate is None else \
            (", ".join(declaration.gate) or "nothing: every version is valid")
        unit = "%" if metric.scale == "ratio" else ""
        print(f"\n{task}")
        print(f"  ranked by    {metric.name} ({'higher' if metric.direction == 'max' else 'lower'} is better; "
              f"{metric.scale}; the best-so-far curve starts at: {metric.anchor})")
        print(f"  valid when   {gate}")
        if declaration.confirm:
            print(f"  new best     scored {declaration.confirm.samples} more time(s), ranked by the "
                  f"{declaration.confirm.aggregate}")
        print(f"  sessions     at most {settings.max_sessions}; stops earlier when the last {settings.window_k} "
              f"bring no gain above {settings.min_improvement:g}{unit}")
        if settings.selection is not None or settings.agents_per_round > 1:
            print(f"  rounds       {settings.agents_per_round} agents; selection {settings.selection or 'greedy'}; "
                  f"global job cap {config.max_parallel}")
        print(f"  start        {settings.start}; inherits: {settings.carry}; limit {limit / 60:g} min per session")
        print(f"  agent        {getattr(adapter, 'task_agent', lambda task: settings.agent)(task)}")
        if args.prompt:
            text = prompts.session_prompt(
                text=adapter.prompt_text(task), instruction=adapter.instruction(task), declaration=declaration,
                parent=None, nodes=[], carry=settings.carry, report_chars=settings.report_chars,
                location=adapter.archive_location, hidden=settings.hide_scores, rules=settings.prompt_rules)
            print(f"\n----- first prompt of {task} -----\n{text}-----")
    return 0


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
    root = prepare_run(args)
    initialize(root)
    store, config, adapter, _ = opened(str(root))
    state = Scheduler(store, config, adapter, log=lambda line: print(line, flush=True)).run()
    print((store.root / "summary.md").read_text(), end="")
    return 0 if not state.get("paused") else 2


def cmd_status(args) -> int:
    store, config, _, tasks = opened(args.campaign, args.name)
    print(json.dumps(campaign_state(store, config, tasks), indent=1))
    return 0


def cmd_report(args) -> int:
    store, _, _, tasks = opened(args.campaign, args.name)
    for task in tasks:
        if store.task(task).declaration() is not None:
            write_matrix(store.task(task))
    print(summary_markdown(write_summary(store, tasks)), end="")
    if args.task:
        print(f"\n## {args.task}\n")
        print((store.task(args.task).archive_dir / "matrix.md").read_text(), end="")
    return 0


def cmd_export(args) -> int:
    store, _, _, tasks = opened(args.campaign, args.name)
    print(f"exported to {export(store, tasks)}")
    return 0


def cmd_reset(args) -> int:
    """Start a task over: delete its directory and record the reset (its earlier events stop counting)."""
    store, _, _, tasks = opened(args.campaign, args.name)
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
    store, _, _, tasks = opened(args.campaign, args.name)
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
    init = sub.add_parser("init", help="create a task directory: task.yaml, evaluate.py, workspace/")
    init.add_argument("task", help="task directory to create")
    init.set_defaults(func=cmd_init)
    new = sub.add_parser("new", help="create <campaign>/config.yaml from a template")
    new.add_argument("campaign", help="campaign directory to create")
    new.add_argument("--template", help="a config.yaml to copy (default: the commented template)")
    new.set_defaults(func=cmd_new)
    for name, func, text in (("check", cmd_check, "say what a run would do, without starting anything"),
                             ("run", cmd_run, "run every task until it stops (re-attaches to running jobs)"),
                             ("status", cmd_status, "print the campaign state"),
                             ("report", cmd_report, "rewrite and print the summary (and one task's matrix)"),
                             ("export", cmd_export, "write lineage graphs and CSVs to exports/"),
                             ("reset", cmd_reset, "delete one task's sessions so it starts over"),
                             ("flag", cmd_flag, "record a finding about a node (shown to later sessions)"),
                             ("clear", cmd_clear, "rank a flagged node again after checking it by hand")):
        command = sub.add_parser(name, help=text)
        command.add_argument("campaign", help="a campaign directory, or a task directory")
        command.add_argument("--name", help="for a task directory: which of its runs (default: default)")
        if name == "check":
            command.add_argument("--prompt", action="store_true", help="also print each task's first prompt")
        if name in ("run", "check"):
            command.add_argument("--agent", help="for a task directory's first run: claude/<model>[:<effort>] or "
                                                 "codex/<model>[:<effort>]")
            command.add_argument("--agent-command", help="... or a shell command that runs any other agent")
            command.add_argument("--sessions", type=int, help="... the most sessions to run (default 10)")
            command.add_argument("--start", choices=("best", "explore", "baseline"), help="... where sessions start")
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
