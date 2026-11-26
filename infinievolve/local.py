"""The built-in adapter `local`: every job is a process on this machine.

A task is a directory:

    <task>/
      task.yaml      what the task is (below)
      workspace/     the baseline: the files every session starts from
      ...            whatever your evaluator needs, e.g. evaluate.py

    # task.yaml
    instruction: |                      what the agent is asked to do
      ...
    evaluate: python3 "$EVOLVE_TASK/evaluate.py"    run in a clean copy of the work; a string (a shell
                                                    command) or a list (a program and its arguments)
    metric: {name: score, direction: max}           the one number that ranks sessions
    # optional:
    declaration: {...}                  the full declaration, instead of `metric`, for several metrics,
                                        a gate, repeat scoring (see the README)
    prompt: {goal, working, ranking, feedback, persistence, continuing}    sentences of the prompt
    time_limit_seconds: 3600            per agent session
    evaluate_timeout_seconds: 600
    workspace: workspace                the baseline directory
    agent: ...                          the agent to use when the campaign names none

The evaluator prints, as its last line, one JSON object:
    {"checks": {"tests_pass": true}, "metrics": {"score": 0.82}, "reasons": []}
Unless the task declares a gate, a version is valid when every check it reports is true.

A session copies the baseline into a fresh directory, applies the work it starts from, runs the agent
there, captures what changed as a patch against the baseline, and evaluates that patch in a second, clean
copy, so the score belongs to the archived work and to nothing else the agent left lying around.

The agent (the campaign's `agent:`, or the task's) is one of

    claude/<model>[:<effort>]     Claude Code, without interaction
    codex/<model>[:<effort>]      Codex, without interaction
    {command: "<shell command>"}  anything else

and is run in the working copy with the prompt on its standard input and, in its environment,
EVOLVE_PROMPT (the prompt's path), EVOLVE_ARCHIVE (the full archive of earlier sessions, "" when nothing
is carried over), EVOLVE_WORKSPACE (the working copy), EVOLVE_TASK (the task directory) and
EVOLVE_CAMPAIGN. Whatever it prints is kept as the session's transcript.

The two named agents run with their permission checks bypassed: they edit files and run commands without
asking, as the user running the campaign, and nothing confines them to the working copy. Run campaigns
where you would let an agent do that.

Needs git. `python -m infinievolve.local run <session directory>` is the program each job runs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import yaml

from . import transcripts
from .core.adapter import AgentUnavailable, JobFailed, PromptText
from .core.archive import CampaignStore
from .core.prompt import extract_report
from .core.types import ScoreRow, SessionResult

# The agent's output, kept in a session's raw/. agent.jsonl is the name the watch page (EvolveVista) and the
# other adapters use; runs made before it was adopted kept it as agent.log, which a resumed run still reads.
TRANSCRIPT = "agent.jsonl"
OLD_TRANSCRIPT = "agent.log"


def transcript_path(raw: Path) -> Path:
    """The agent's output in a session's raw/: agent.jsonl, or agent.log in a run made before that name."""
    old = raw / OLD_TRANSCRIPT
    return old if old.is_file() and not (raw / TRANSCRIPT).is_file() else raw / TRANSCRIPT


GIT = ["git", "-c", "user.name=infinievolve", "-c", "user.email=infinievolve@localhost", "-c", "commit.gpgsign=false"]
TASKS_DIR = "task-defs"   # options.tasks_dir when unset; never the campaign's own tasks/, which is its archive
WORKING = ("Work in the directory you are started in. Whatever differs from its starting state when you end "
           "the session is your work.")


def agent_command(agent) -> tuple:
    """(command, whether it is a shell command) for an agent setting."""
    if isinstance(agent, dict) and agent.get("command"):
        return str(agent["command"]), True
    spec = agent.get("spec") if isinstance(agent, dict) else agent
    if not isinstance(spec, str) or "/" not in spec:
        raise ValueError("agent must be `claude/<model>[:<effort>]`, `codex/<model>[:<effort>]` or "
                         f"{{command: <shell command>}}, not {agent!r}")
    program, rest = spec.split("/", 1)
    model, _, effort = rest.partition(":")
    if program == "claude":
        return ["claude", "-p", "--model", model, *(["--effort", effort] if effort else []),
                "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose",
                "--no-session-persistence"], False
    if program == "codex":
        return ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                "-m", model, *(["-c", f'model_reasoning_effort="{effort}"'] if effort else []), "-"], False
    raise ValueError(f"unknown agent program {program!r}: use claude/..., codex/... or {{command: ...}}")


# -- the job itself (runs in its own process) ----------------------------------------------
def git(directory: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([*GIT, *args], cwd=directory, input=stdin, capture_output=True)


def fresh_copy(baseline: Path, target: Path, patch: bytes) -> str | None:
    """A copy of the baseline with the patch applied, as a repository of its own. Returns why it failed."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(baseline, target)
    git(target, "init", "-q")
    (target / ".git" / "info" / "exclude").write_text("__pycache__/\n")
    git(target, "add", "-A")
    git(target, "commit", "-q", "--allow-empty", "-m", "baseline")
    if patch.strip():
        applied = git(target, "apply", "--binary", "-", stdin=patch)
        if applied.returncode:
            return "the work does not apply to the baseline: " + applied.stderr.decode(errors="replace").strip()[-500:]
    return None


def capture(directory: Path) -> bytes:
    """Everything that differs from the baseline, new files included."""
    git(directory, "add", "-A")
    return git(directory, "diff", "--cached", "--binary", "HEAD").stdout


def evaluate(request: dict, patch: bytes, scratch: Path) -> dict:
    """Score a patch in a clean copy of the baseline."""
    problem = fresh_copy(Path(request["baseline"]), scratch, patch)
    if problem:
        return {"checks": {"applies": False}, "metrics": {}, "reasons": [problem]}
    command = request["evaluate"]
    try:
        done = subprocess.run(command, shell=isinstance(command, str), cwd=scratch, capture_output=True, text=True,
                              env={**os.environ, **request["environment"]}, timeout=request["evaluate_timeout_seconds"])
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        row = json.loads(lines[-1])
        return {"checks": dict(row.get("checks") or {}), "metrics": dict(row.get("metrics") or {}),
                "reasons": list(row.get("reasons") or [])}
    except subprocess.TimeoutExpired:
        return {"checks": {"evaluated": False}, "metrics": {}, "reasons": ["the evaluation timed out"]}
    except (IndexError, ValueError, AttributeError):
        return {"checks": {"evaluated": False}, "metrics": {}, "reasons": ["the evaluation printed no result"]}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_job(session_dir: Path) -> None:
    request = json.loads((session_dir / "input" / "request.json").read_text())
    raw = session_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    timing, outcome = {"where": "local"}, {}
    start = Path(request["start"]).read_bytes() if request.get("start") else b""
    if request["kind"] == "session":
        work = session_dir / "work"
        problem = fresh_copy(Path(request["baseline"]), work, start)
        if problem:
            raise SystemExit(problem)
        environment = {**os.environ, **request["environment"], "EVOLVE_PROMPT": request["prompt"],
                       "EVOLVE_ARCHIVE": request["archive"], "EVOLVE_WORKSPACE": str(work)}
        began = time.time()
        with (raw / TRANSCRIPT).open("w") as log, open(request["prompt"], "rb") as prompt:
            try:
                done = subprocess.run(request["agent_command"], shell=request["agent_shell"], cwd=work, env=environment,
                                      stdin=prompt, stdout=log, stderr=subprocess.STDOUT,
                                      timeout=request["time_limit_seconds"])
                outcome = {"agent_exit": done.returncode, "deadline_stop": False}
            except subprocess.TimeoutExpired:
                outcome = {"agent_exit": None, "deadline_stop": True}
        timing["agent_seconds"] = round(time.time() - began, 1)
        patch = capture(work)
        shutil.rmtree(work / ".git", ignore_errors=True)
    else:   # "baseline" scores the untouched baseline; "rescore" scores archived work again
        patch = start
    (raw / "final.patch").write_bytes(patch)
    began = time.time()
    row = evaluate(request, patch, session_dir / "check")
    timing["evaluate_seconds"] = round(time.time() - began, 1)
    temporary = raw / "result.json.tmp"
    temporary.write_text(json.dumps({"row": row, "timing": timing, "outcome": outcome}, indent=1, sort_keys=True))
    temporary.replace(raw / "result.json")   # last: its presence says the job is over


# -- the adapter (runs in the scheduler) ---------------------------------------------------
class LocalAdapter:
    archive_location = "the file whose path is in the environment variable `EVOLVE_ARCHIVE`"

    def __init__(self, config) -> None:
        self.options = dict(config.options)
        self.campaign = Path(config.root or ".").resolve()
        self.agent = config.defaults.agent
        archive = CampaignStore(self.campaign).tasks_dir.resolve()
        if self.options.get("task_dir"):      # one task: the directory itself
            key = "task_dir"
            root = directory = (self.campaign / self.options["task_dir"]).resolve()
            self.tasks = {directory.name: directory}
        else:                                 # several: every directory with a task.yaml
            key = "tasks_dir"
            root = (self.campaign / self.options.get("tasks_dir", TASKS_DIR)).resolve()
            if "tasks_dir" not in self.options and not any(root.glob("*/task.yaml")) \
                    and any(archive.glob("*/task.yaml")):
                root = archive                # written when the default was tasks/: refused below, with the way out
            self.tasks = {path.parent.name: path.parent for path in sorted(root.glob("*/task.yaml"))}
        if root == archive or archive in root.parents:
            setting = f"options.{key}" if key in self.options else "This campaign"
            raise ValueError(
                f"{setting} puts task directories in {archive}, where the campaign archives each task's "
                f"sessions; `infinievolve reset` deletes a task's directory there, task.yaml and workspace/ "
                f"included. Move each task's own files (all but sessions/, archive/, rounds/ and task.json) to "
                f"{self.campaign / TASKS_DIR}/<task>/ and set `options: {{tasks_dir: {TASKS_DIR}}}` in config.yaml")
        self._children: dict[str, subprocess.Popen] = {}

    # -- what the tasks are -----------------------------------------------------------
    def spec(self, task: str) -> dict:
        return yaml.safe_load((self.tasks[task] / "task.yaml").read_text()) or {}

    def resolve_tasks(self, selection) -> list[str]:
        if selection == "all":
            return sorted(self.tasks)
        missing = [task for task in selection if task not in self.tasks]
        if missing:
            raise ValueError(f"unknown tasks {missing}; found {sorted(self.tasks)}")
        return list(selection)

    def declaration(self, task: str) -> dict:
        spec = self.spec(task)
        if "declaration" in spec:
            return {"artifact": "patch", **spec["declaration"]}
        metric = dict(spec.get("metric") or {})
        if "name" not in metric:
            raise ValueError(f"{task}/task.yaml needs `metric: {{name: ..., direction: max | min}}` or a `declaration`")
        return {"metrics": {str(metric.pop("name")): metric}, "artifact": "patch"}

    def instruction(self, task: str) -> str:
        return str(self.spec(task)["instruction"]).strip()

    def prompt_text(self, task: str) -> PromptText:
        return PromptText(**{"working": WORKING, **(self.spec(task).get("prompt") or {})})

    def default_time_limit_seconds(self, task: str) -> int:
        return int(self.spec(task).get("time_limit_seconds", 3600))

    def baseline_problem(self, task: str, root) -> str | None:
        reasons = [reason for row in root.scores for reason in row.get("reasons") or []]
        broken = [reason for reason in reasons if "printed no result" in reason or "timed out" in reason]
        return broken[0] if broken else None

    def task_agent(self, task: str):
        agent = self.agent or self.spec(task).get("agent")
        if not agent:
            raise ValueError("no agent: set `agent:` in the campaign's config or in the task's task.yaml")
        return agent

    def provenance(self, tasks: list[str]) -> dict:
        return {"tasks": {task: str(self.tasks[task]) for task in tasks},
                "agent": {task: self.task_agent(task) for task in tasks}}

    # -- jobs -------------------------------------------------------------------------
    def _launch(self, key: str, task: str, kind: str, directory: Path, **extra) -> dict:
        spec, home = self.spec(task), self.tasks[task]
        command = spec["evaluate"]
        if not isinstance(command, str):
            command = [str(part).format(task=home, python=sys.executable) for part in command]
        request = {"kind": kind, "baseline": str(home / spec.get("workspace", "workspace")), "evaluate": command,
                   "evaluate_timeout_seconds": int(spec.get("evaluate_timeout_seconds", 600)),
                   "environment": {"EVOLVE_TASK": str(home), "EVOLVE_CAMPAIGN": str(self.campaign)}, **extra}
        if kind == "session":
            request["agent_command"], request["agent_shell"] = agent_command(self.task_agent(task))
        directory = directory.resolve()
        (directory / "input").mkdir(parents=True, exist_ok=True)
        (directory / "raw").mkdir(parents=True, exist_ok=True)
        (directory / "input" / "request.json").write_text(json.dumps(request, indent=1, sort_keys=True))
        log = (directory / "raw" / "runner.log").open("w")
        child = subprocess.Popen([sys.executable, "-m", "infinievolve.local", "run", str(directory)],
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self._children[key] = child
        return {"pid": child.pid, "directory": str(directory)}

    def submit_baseline(self, task: str, session_dir: Path, events) -> dict:
        return self._launch(f"{task}/s000", task, "baseline", session_dir)

    def submit_session(self, spec, session_dir: Path, events) -> dict:
        inputs = session_dir.resolve() / "input"
        archive = inputs / "archive.md"
        start = next(iter(sorted(inputs.glob("start.*"))), None) if spec.start_artifact else None
        return self._launch(f"{spec.task}/{spec.session_id}", spec.task, "session", session_dir,
                            prompt=str(inputs / "prompt.md"), archive=str(archive) if archive.is_file() else "",
                            start=str(start) if start else None, time_limit_seconds=spec.time_limit_seconds)

    def submit_rescore(self, task: str, name: str, artifact: bytes, job_dir: Path, events, avoid=()) -> dict:
        (job_dir / "input").mkdir(parents=True, exist_ok=True)
        (job_dir / "input" / "start.patch").write_bytes(artifact)
        return self._launch(f"{task}/{name}", task, "rescore", job_dir,
                            start=str((job_dir / "input" / "start.patch").resolve()))

    def _over(self, job) -> bool:
        if (Path(job.handle["directory"]) / "raw" / "result.json").is_file():
            return True
        child = self._children.get(job.key)
        if child is not None:
            return child.poll() is not None
        try:   # a job of an earlier scheduler process: not ours to wait for, only to look at
            os.kill(job.handle["pid"], 0)
            return False
        except OSError:
            return True

    def poll(self, jobs, events) -> set[str]:
        return {job.key for job in jobs if self._over(job)}

    def collect(self, job, session_dir: Path, events) -> SessionResult:
        self._children.pop(job.key, None)
        raw = session_dir / "raw"
        if not (raw / "result.json").is_file():
            log = (raw / "runner.log").read_text(errors="replace")[-2000:] if (raw / "runner.log").is_file() else ""
            shutil.rmtree(raw, ignore_errors=True)
            shutil.rmtree(session_dir / "work", ignore_errors=True)
            raise JobFailed(f"the job ended without a result\n{log}")
        result = json.loads((raw / "result.json").read_text())
        row = ScoreRow(label="final", checks=result["row"]["checks"], metrics=result["row"]["metrics"],
                       reasons=result["row"]["reasons"])
        artifacts = {"final": (raw / "final.patch").read_bytes()}
        if job.kind != "session":
            return SessionResult(scores=[row], artifacts=artifacts, timing=result["timing"])
        transcript = transcript_path(raw)
        refused = transcripts.refusal(transcript)
        if refused:
            shutil.rmtree(raw, ignore_errors=True)
            shutil.rmtree(session_dir / "work", ignore_errors=True)
            raise AgentUnavailable(f"the agent's service refused the session: {refused[0]}", refused[1])
        return SessionResult(scores=[row], artifacts=artifacts, report=extract_report(transcripts.texts(transcript)),
                             timing=result["timing"], usage=transcripts.usage(transcript),
                             stats={**result["outcome"], "tool_calls": len(transcripts.tool_calls(transcript))})


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "run":
        raise SystemExit("usage: python -m infinievolve.local run <session directory>")
    run_job(Path(sys.argv[2]))
