"""Campaign configuration: <campaign>/config.yaml (the campaign's name is its directory name).

    campaign:    adapter (a registered name, an entry point, `package.module:Name` or `path/to/file.py:Name`),
                 tasks (a list, or "all"), max_parallel (jobs in flight across tasks), poll_seconds,
                 agents_per_round (default 1),
                 session_minutes (optional hard cap per agent session; default from each task),
                 name (optional; must equal the directory name)
    agent:       settings of the agent, free-form: a mapping, or one string (kept as {spec: <string>}). The
                 framework records them on every node and hands them to the adapter; it reads none itself.
    strategy:    start (legacy best | explore | baseline), seed, sharpness (legacy explore only),
                 selection (greedy | probabilistic | genetic), temperature, population_size,
                 elite_count, crossover_rate, mutation_rate
    stop:        max_sessions (n), window_k (k), min_improvement (x: a percentage when the ranking metric
                 is a ratio, an amount when it is absolute)
    prompt:      report_chars, carry (full | reports | none), rules (extra rules shown to every session),
                 hide_scores
    evaluation:  overrides of the adapter's declaration: gate, rank_by, show, confirm
    task_overrides:  {<task>: {session_minutes, start, max_sessions, window_k, min_improvement, carry,
                               hide_scores, hold, evaluation}}
                 hold: true submits no new session for the task (running jobs finish and are archived)
                 hide_scores: true keeps archived scores out of what later sessions read, for a task scored
                 on something its sessions must not learn from; ranking still uses them
    options:     passed to the adapter as they are
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import math

import yaml

from .core.selection import STRATEGIES

# What a session inherits from the ones before it, independently of what it starts from:
#   full     the archive as it is: the table, every report, every session's archived work
#   reports  the same without the archived work, so earlier work cannot be copied out of the archive
#   none     no archive at all, and the prompt never mentions one
ROUND_KEYS = ("agents_per_round", "selection", "temperature", "population_size", "elite_count",
              "crossover_rate", "mutation_rate")
CARRY = ("full", "reports", "none")
OVERRIDABLE = ("session_minutes", "start", "max_sessions", "window_k", "min_improvement", "carry", "hide_scores",
               "hold", "evaluation") + ROUND_KEYS


@dataclass(frozen=True)
class TaskSettings:
    """The settings one task's loop runs with: campaign defaults plus that task's overrides."""
    task: str
    session_minutes: float | None
    start: str
    seed: int
    sharpness: float
    max_sessions: int
    window_k: int
    min_improvement: float
    report_chars: int
    agents_per_round: int = 1
    selection: str | None = None
    temperature: float = 1.0
    population_size: int | None = None
    elite_count: int = 1
    crossover_rate: float = 0.8
    mutation_rate: float = 0.2
    carry: str = "full"
    hide_scores: bool = False
    hold: bool = False
    prompt_rules: tuple = ()
    evaluation: dict = field(default_factory=dict)
    agent: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CampaignConfig:
    name: str
    adapter: str
    tasks: tuple | str                    # explicit list, or "all" (resolved by the adapter)
    defaults: TaskSettings                # its task field is unused
    root: Path | None = None              # the campaign directory, for adapters given as a relative path
    max_parallel: int = 4
    poll_seconds: float = 60
    overrides: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)

    def for_task(self, task: str) -> TaskSettings:
        return replace(self.defaults, task=task, **self.overrides.get(task, {}))


def check_settings(settings: TaskSettings, where: str) -> None:
    for key in ("agents_per_round", "population_size", "elite_count"):
        value = getattr(settings, key)
        if key == "population_size" and value is None:
            continue
        if type(value) is not int or value < (0 if key == "elite_count" else 1):
            raise ValueError(f"{where}{key} must be an integer >= {0 if key == 'elite_count' else 1}")
    if settings.elite_count > (settings.population_size or settings.agents_per_round):
        raise ValueError(f"{where}elite_count must not exceed population_size")
    if settings.selection not in (None, "greedy", "probabilistic", "genetic"):
        raise ValueError(f"{where}selection must be greedy, probabilistic, or genetic")
    if (settings.selection is not None or settings.agents_per_round > 1) and settings.start != "best":
        raise ValueError(f"{where}round mode requires default start: best; use strategy.selection instead")
    for key in ("temperature", "crossover_rate", "mutation_rate"):
        value = getattr(settings, key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{where}{key} must be finite")
        if (key == "temperature" and value <= 0) or (key != "temperature" and not 0 <= value <= 1):
            raise ValueError(f"{where}{key} is out of range")
    if settings.start not in STRATEGIES:
        raise ValueError(f"{where}start must be one of {STRATEGIES}")
    if settings.carry not in CARRY:
        raise ValueError(f"{where}carry must be one of {CARRY}")
    if settings.max_sessions < 1 or settings.window_k < 1 or settings.min_improvement < 0:
        raise ValueError(f"{where}stop needs max_sessions >= 1, window_k >= 1, min_improvement >= 0")
    if settings.session_minutes is not None and settings.session_minutes <= 0:
        raise ValueError(f"{where}session_minutes must be positive")
    if not isinstance(settings.evaluation, dict):
        raise ValueError(f"{where}evaluation must be a mapping")


def parse_config(data: dict, name: str | None = None, root: Path | None = None) -> CampaignConfig:
    known = {"campaign", "agent", "strategy", "stop", "prompt", "evaluation", "task_overrides", "options"}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"config: unknown sections {sorted(unknown)}; known {sorted(known)}")
    campaign, strategy, stop = data.get("campaign") or {}, data.get("strategy") or {}, data.get("stop") or {}
    prompt = data.get("prompt") or {}
    if name is not None:
        if campaign.get("name") not in (None, name):
            raise ValueError(f"campaign.name {campaign['name']!r} differs from the campaign directory {name!r}")
        campaign = {**campaign, "name": name}
    for key in ("name", "adapter", "tasks"):
        if key not in campaign:
            raise ValueError(f"campaign: is missing {key}")
    tasks = campaign["tasks"]
    if tasks != "all":
        if isinstance(tasks, str):
            tasks = [tasks]
        if not tasks or len(set(tasks)) != len(tasks):
            raise ValueError("campaign.tasks must be 'all' or a non-empty list without duplicates")
        tasks = tuple(str(task) for task in tasks)
    minutes = campaign.get("session_minutes")
    defaults = TaskSettings(
        task="", session_minutes=float(minutes) if minutes is not None else None,
        start=str(strategy.get("start", "best")), seed=int(strategy.get("seed", 0)),
        sharpness=float(strategy.get("sharpness", 2.0)),
        max_sessions=int(stop.get("max_sessions", 10)), window_k=int(stop.get("window_k", 3)),
        min_improvement=float(stop.get("min_improvement", 0.0)),
        agents_per_round=campaign.get("agents_per_round", 1),
        selection=strategy.get("selection"), temperature=strategy.get("temperature", 1.0),
        population_size=strategy.get("population_size"), elite_count=strategy.get("elite_count", 1),
        crossover_rate=strategy.get("crossover_rate", 0.8), mutation_rate=strategy.get("mutation_rate", 0.2),
        report_chars=int(prompt.get("report_chars", 4000)), carry=str(prompt.get("carry", "full")),
        hide_scores=bool(prompt.get("hide_scores", False)),
        prompt_rules=tuple(str(rule) for rule in prompt.get("rules") or ()),
        evaluation=dict(data.get("evaluation") or {}),
        agent={"spec": data["agent"]} if isinstance(data.get("agent"), str) else dict(data.get("agent") or {}))
    check_settings(defaults, "")
    overrides = {}
    for task, values in (data.get("task_overrides") or {}).items():
        extra = set(values) - set(OVERRIDABLE)
        if extra:
            raise ValueError(f"task_overrides.{task}: unsupported keys {sorted(extra)}; allowed {OVERRIDABLE}")
        overrides[str(task)] = dict(values)
    config = CampaignConfig(name=str(campaign["name"]), adapter=str(campaign["adapter"]), tasks=tasks,
                            defaults=defaults, root=root, max_parallel=int(campaign.get("max_parallel", 4)),
                            poll_seconds=float(campaign.get("poll_seconds", 60)), overrides=overrides,
                            options=dict(data.get("options") or {}))
    for task in overrides:
        check_settings(config.for_task(task), f"task_overrides.{task}.")
    if config.max_parallel < 1:
        raise ValueError("campaign.max_parallel must be >= 1")
    if not config.name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("the campaign's name may only use letters, digits, '-' and '_'")
    return config


def campaign_config(root: Path) -> CampaignConfig:
    root = Path(root).resolve()
    data = yaml.safe_load((root / "config.yaml").read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{root / 'config.yaml'} must hold a YAML mapping")
    return parse_config(data, name=root.name, root=root)
