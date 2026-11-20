"""A minimal adapter: every job is a process on this machine. Copy it and change it.

A task is a directory:

    <tasks_dir>/<task>/
      task.yaml      instruction, declaration, prompt, evaluate, time_limit_seconds
      workspace/     the baseline: what a session starts from

A session copies the baseline into a fresh directory, applies the work it starts from, runs the
campaign's `options.agent_command` there, captures what changed as a patch against the baseline, and
evaluates that patch in a second, clean copy -- so the score belongs to the archived work and to nothing
else the agent left lying around.

The agent command is a shell command run in the working copy. It finds its inputs in the environment:

    EVOLVE_PROMPT     path of the session prompt
    EVOLVE_ARCHIVE    path of the full archive of earlier sessions ("" when nothing is carried over)
    EVOLVE_WORKSPACE  the working copy (also the current directory)
    EVOLVE_CAMPAIGN   the campaign directory

and whatever it prints is kept as the session's transcript, from which the report is read.

The evaluate command is run in the clean copy and must print, as its last line, one JSON object:
{"checks": {name: bool}, "metrics": {name: number}, "reasons": [text]}.

Needs git. This file is also the program each job runs: `python adapter.py run <session directory>`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

GIT = ["git", "-c", "user.name=infinievolve", "-c", "user.email=infinievolve@localhost", "-c", "commit.gpgsign=false"]


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
        return {"checks": {}, "metrics": {}, "reasons": [problem]}
    try:
        done = subprocess.run(request["evaluate"], cwd=scratch, capture_output=True, text=True,
                              timeout=request["evaluate_timeout_seconds"])
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        row = json.loads(lines[-1])
        return {"checks": dict(row.get("checks") or {}), "metrics": dict(row.get("metrics") or {}),
                "reasons": list(row.get("reasons") or [])}
    except subprocess.TimeoutExpired:
        return {"checks": {}, "metrics": {}, "reasons": ["the evaluation timed out"]}
    except (IndexError, ValueError, AttributeError):
        return {"checks": {}, "metrics": {}, "reasons": ["the evaluation printed no result"]}
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
        environment = {**os.environ, "EVOLVE_PROMPT": request["prompt"], "EVOLVE_ARCHIVE": request["archive"],
                       "EVOLVE_WORKSPACE": str(work), "EVOLVE_CAMPAIGN": request["campaign"]}
        began = time.time()
        with (raw / "agent.log").open("w") as log:
            try:
                done = subprocess.run(request["agent_command"], shell=True, cwd=work, env=environment, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=request["time_limit_seconds"])
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
    result = {"row": row, "timing": timing, "outcome": outcome}
    temporary = raw / "result.json.tmp"
    temporary.write_text(json.dumps(result, indent=1, sort_keys=True))
    temporary.replace(raw / "result.json")   # last: its presence says the job is over


# -- the adapter (runs in the scheduler) ---------------------------------------------------
class LocalAdapter:
    archive_location = "the file whose path is in the environment variable `EVOLVE_ARCHIVE`"

    def __init__(self, config) -> None:
        import yaml
        from infinievolve.core.adapter import PromptText
        self._yaml, self._prompt_text = yaml, PromptText
        self.options = dict(config.options)
        self.campaign = Path(config.root or ".").resolve()
        self.tasks_dir = (self.campaign / self.options.get("tasks_dir", "tasks")).resolve()
        self.agent_command = self.options.get("agent_command")
        self._children: dict[str, subprocess.Popen] = {}

    # -- what the tasks are -----------------------------------------------------------
    def task_dir(self, task: str) -> Path:
        return self.tasks_dir / task

    def spec(self, task: str) -> dict:
        return self._yaml.safe_load((self.task_dir(task) / "task.yaml").read_text())

    def resolve_tasks(self, selection) -> list[str]:
        known = sorted(path.parent.name for path in self.tasks_dir.glob("*/task.yaml"))
        if selection == "all":
            return known
        missing = [task for task in selection if task not in known]
        if missing:
            raise ValueError(f"unknown tasks {missing}; {self.tasks_dir} has {known}")
        return list(selection)

    def declaration(self, task: str) -> dict:
        return {"artifact": "patch", **self.spec(task)["declaration"]}

    def instruction(self, task: str) -> str:
        return self.spec(task)["instruction"].strip()

    def prompt_text(self, task: str):
        return self._prompt_text(**self.spec(task)["prompt"])

    def default_time_limit_seconds(self, task: str) -> int:
        return int(self.spec(task).get("time_limit_seconds", 3600))

    def baseline_problem(self, task: str, root) -> str | None:
        reasons = [reason for row in root.scores for reason in row.get("reasons") or []]
        broken = [reason for reason in reasons if "printed no result" in reason or "timed out" in reason]
        return broken[0] if broken else None

    def provenance(self, tasks: list[str]) -> dict:
        return {"tasks_dir": str(self.tasks_dir), "agent_command": self.agent_command}

    # -- jobs -------------------------------------------------------------------------
    def _launch(self, key: str, task: str, kind: str, directory: Path, **extra) -> dict:
        if kind == "session" and not self.agent_command:
            raise ValueError("options.agent_command is not set")
        spec = self.spec(task)
        fill = {"task": str(self.task_dir(task)), "python": sys.executable}
        request = {"kind": kind, "baseline": str(self.task_dir(task) / "workspace"),
                   "evaluate": [str(part).format(**fill) for part in spec["evaluate"]],
                   "evaluate_timeout_seconds": int(spec.get("evaluate_timeout_seconds", 600)),
                   "agent_command": self.agent_command, "campaign": str(self.campaign), **extra}
        directory = directory.resolve()
        (directory / "input").mkdir(parents=True, exist_ok=True)
        (directory / "raw").mkdir(parents=True, exist_ok=True)
        (directory / "input" / "request.json").write_text(json.dumps(request, indent=1, sort_keys=True))
        log = (directory / "raw" / "runner.log").open("w")
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", str(directory)],
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

    def collect(self, job, session_dir: Path, events):
        from infinievolve.core.adapter import JobFailed
        from infinievolve.core.prompt import extract_report
        from infinievolve.core.types import ScoreRow, SessionResult
        self._children.pop(job.key, None)
        raw = session_dir / "raw"
        if not (raw / "result.json").is_file():
            log = (raw / "runner.log").read_text(errors="replace")[-2000:] if (raw / "runner.log").is_file() else ""
            shutil.rmtree(raw, ignore_errors=True)
            shutil.rmtree(session_dir / "work", ignore_errors=True)
            raise JobFailed(f"the job ended without a result\n{log}")
        result = json.loads((raw / "result.json").read_text())
        row = result["row"]
        transcript = (raw / "agent.log").read_text(errors="replace") if (raw / "agent.log").is_file() else ""
        return SessionResult(
            scores=[ScoreRow(label="final", checks=row["checks"], metrics=row["metrics"], reasons=row["reasons"])],
            artifacts={"final": (raw / "final.patch").read_bytes()},
            report=extract_report(transcript) if job.kind == "session" else "",
            timing=result["timing"], stats=result["outcome"])


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "run":
        raise SystemExit("usage: adapter.py run <session directory>")
    run_job(Path(sys.argv[2]))
