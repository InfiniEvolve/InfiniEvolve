"""An adapter for fai-bench task packages on an HTCondor cluster reached over ssh.

A fai-bench task is a package directory (instruction.md, task.toml, pre_artifacts.sh, tests/) plus a
prebuilt Apptainer image holding the working tree at /app/repo. Each job is one HTCondor job that runs
job.py (beside this file) on a node: it scores the baseline, runs one Claude Code session inside the
image, or scores archived work again. A session's work is a patch against the image's /app/repo.

These are timing tasks, so the adapter builds on infinievolve.task_types.perf: what is declared, the
wording of the prompt and how a result is put together come from there. A task whose package has a timing
workload is ranked by a paired timing: the verified tree and a reference tree, timed in alternation on the
job's own node. The reference is the task's reference solution where the package ships one as a patch,
else the unmodified baseline, else (a baseline that does not run) the campaign's first valid session.
A task whose measure is a count is ranked by the verifier's number as it is.

Everything about your site comes from the campaign's config; nothing is assumed here:

    agent:
      path: /cluster/path/to/claude          the Claude Code binary, readable from the nodes
      model: ...
      effort: ...
      secret_env: /cluster/path/to/token.env a file on the cluster holding CLAUDE_CODE_OAUTH_TOKEN=...
    options:
      repo: /local/path/to/fai-bench         a checkout of the task packages (tasks_index.json at its root)
      ssh_host: login.example.org            where condor_submit runs; reached with `ssh` and `rsync`
      remote_root: /cluster/path/for/jobs    job inputs and outputs live under it
      image_root: /cluster/path/to/images    <task>-<hash>.sif files
      submit_command: condor_submit          (default; e.g. "condor_submit_bid 50" where the pool needs one)
      memory_headroom_mb: 8192               added to the task's own memory request (the agent runs in the job too)
      margin_minutes: 30                     job time beyond the agent's and the verifier's limits
      forbidden: {<flag>: <regular expression>}   a session whose tool calls match is flagged
      flags: {<flag>: soft | hard}           levels of those flags (default hard)

    How results are measured. On by default:
      paired: true                           rank by paired timing where a task can be paired (false: the
                                             verifier's own speedup)
      confirm_samples: 2                     score a new best this many more times (0: never)
      pairing: {blocks: 4, budget_seconds: 240}
    Off unless you set them:
      requirements: 'TARGET.CPUModel == "..."'   a ClassAd expression every job must meet, e.g. one
                                             processor model, so that timings are comparable
      cpu_model: "..."                       measurements taken on another processor model are taken again
      hosts_file: path/to/hosts.yaml         a ledger of machines: one whose measurement did not hold up
                                             elsewhere gets a strike, two strikes and it is not used again
      wallclock: true                        time the workload by the wall clock even where it reads CPU time
      reap_processes: true                   end what the agent or a timing left running before the next
                                             step. This kills processes (only ones started by the job).

What it leaves out: tasks that write outside /app/repo, the tasks' sealed dev loop (the agent's container
can read the image's hidden tests, so use `forbidden` to notice it), GPU health checks, and more than one
account. It is an example of the interface, not a replacement for the benchmark's own runner.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tomllib

from infinievolve import transcripts
from infinievolve.core.adapter import AgentUnavailable, JobFailed
from infinievolve.core.prompt import extract_report
from infinievolve.core.types import SessionResult
from infinievolve.task_types.perf import measure, policy
from infinievolve.task_types.perf.adapter import PerfAdapter
from infinievolve.task_types.perf.hosts import HostLedger

JOB = Path(__file__).resolve().parent / "job.py"
ARCHIVE = "/run/infinievolve/archive.md"
# reasons the verifier gives for "valid, just not faster": they lower the score, they do not fail a check
NOT_FASTER = re.compile(r"^((ref_)?speedup_not_above|no_speedup|ref_speedup_invalid|invalid_ref_speedup)")
# verifier reasons that can only mean a broken environment when the untouched baseline shows them
BROKEN = {"benchmark_execution_failed", "validation_incomplete", "verifier_missing", "import_origin_failed",
          "baseline_failed", "import_origin_not_app_repo", "repo_missing"}


class FaiBenchAdapter(PerfAdapter):
    archive_location = f"`{ARCHIVE}`"
    gate = ("correctness_ok", "scope_ok", "import_origin_ok", "no_hard_fail")   # the verifier's own gates
    working = ("Edit only the files the task allows. Leave your changes uncommitted in the working tree: never "
               "commit, reset or move HEAD.")

    def __init__(self, config) -> None:
        self.campaign = config.name
        self.options = dict(config.options)
        self.agent = dict(config.defaults.agent)
        missing = [key for key in ("repo", "ssh_host", "remote_root", "image_root") if key not in self.options] + \
                  [f"agent.{key}" for key in ("path", "model", "effort", "secret_env") if key not in self.agent]
        if missing:
            raise ValueError(f"the fai-bench adapter needs these settings: {missing}")
        self.repo = Path(self.options["repo"]).expanduser()
        self.index = {row["task"]: row for row in json.loads((self.repo / "tasks_index.json").read_text())["tasks"]}
        self.host = self.options["ssh_host"]
        self.root = self.options["remote_root"].rstrip("/")
        self.confirm_samples = int(self.options.get("confirm_samples", 2))
        self.ledger = (HostLedger((Path(config.root or ".") / self.options["hosts_file"]).expanduser())
                       if self.options.get("hosts_file") else None)
        self._commit: str | None = None
        self._deployed: set = set()

    # -- the cluster ------------------------------------------------------------------
    def ssh(self, command: str, stdin: str | None = None) -> str:
        done = subprocess.run(["ssh", "-o", "BatchMode=yes", self.host, command], input=stdin, text=True,
                              capture_output=True)
        if done.returncode:
            raise subprocess.CalledProcessError(done.returncode, command, done.stdout, done.stderr[-2000:])
        return done.stdout

    def push(self, local: Path, remote: str, excludes: tuple = ()) -> None:
        subprocess.run(["rsync", "-a", *[f"--exclude={item}" for item in excludes], f"{local}/",
                        f"{self.host}:{remote}/"], check=True, capture_output=True)

    def pull(self, remote: str, local: Path) -> None:
        local.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rsync", "-a", f"{self.host}:{remote}/", f"{local}/"], check=True, capture_output=True)

    def remote_dir(self, task: str, name: str) -> str:
        return f"{self.root}/experiments/{self.campaign}/{task}/{name}"

    # -- what the tasks are -----------------------------------------------------------
    def package(self, task: str) -> Path:
        return self.repo / self.index[task]["package_root"]

    def toml(self, task: str) -> dict:
        return tomllib.loads((self.package(task) / "task.toml").read_text())

    def resources(self, task: str) -> dict:
        environment = self.toml(task).get("environment", {})
        return {"gpus": int(environment.get("gpus", 0) or 0), "cpus": int(environment.get("cpus", 8)),
                "memory_mb": int(environment.get("memory_mb", 32768))}

    def resolve_tasks(self, selection) -> list[str]:
        if selection == "all":
            return sorted(self.index)
        unknown = [task for task in selection if task not in self.index]
        if unknown:
            raise ValueError(f"not in tasks_index.json: {unknown}")
        return list(selection)

    def instruction(self, task: str) -> str:
        return (self.package(task) / "instruction.md").read_text()

    def default_time_limit_seconds(self, task: str) -> int:
        return int(self.toml(task).get("agent", {}).get("timeout_sec", 5400))

    # -- what the timing layer asks about a task -----------------------------------------
    def exact(self, task: str) -> bool:
        """The task's measure is a count (instructions, bytes, operations): the same number every time."""
        name = str(self.toml(task).get("primary_metric", {}).get("name", "speedup"))
        return not (name == "speedup" or name.endswith("gpu_speedup"))

    def oracle(self, task: str) -> Path | None:
        """The task's reference solution, where the package ships exactly one, as a patch."""
        found = [self.package(task) / name for name in self.index[task].get("oracle_artifacts") or []]
        return found[0] if len(found) == 1 and found[0].suffix == ".patch" and found[0].is_file() else None

    def can_pair(self, task: str) -> bool:
        workload = self.package(task) / "tests" / "workload.py"
        return bool(self.options.get("paired", True)) and not self.exact(task) and workload.is_file() \
            and "timing_ms" in workload.read_text(errors="ignore")

    def pairs_with(self, task: str) -> str | None:
        if not self.can_pair(task):
            return None
        if self.oracle(task):
            return "a reference solution of the task"
        return "the unmodified baseline (or, when that does not run, the first valid session's version)"

    def paired_solution(self, task: str) -> str | None:
        return "oracle" if self.can_pair(task) and self.oracle(task) else None

    def hardware(self, task: str) -> str:
        resources = self.resources(task)
        return f"{resources['cpus']} CPU cores" + (" and one GPU" if resources["gpus"] else "")

    def feedback(self, task: str) -> str:
        submissions = int(self.toml(task).get("loop", {}).get("max_submissions", 0) or 0)
        if submissions > 1:
            return (f"The task's own tool gives feedback during the session: `bash /opt/loop/submit.sh` scores the "
                    f"working tree, up to {submissions} times. The last allowed call finalizes the tree and ends the "
                    "session, so keep it for your best version.")
        if submissions == 1:
            return ("**Submitting ends this session.** The task allows exactly one call of "
                    "`bash /opt/loop/submit.sh`; iterate with your own tests and timings first.")
        return "The task has no submission tool: check correctness and speed with your own tests and timings."

    def flag_levels(self) -> dict:
        return dict(self.options.get("flags") or {})

    def baseline_problem(self, task: str, root) -> str | None:
        if not root.scores:
            return "no verdict"
        reasons = {str(reason).split(":")[0] for row in root.scores for reason in row.get("reasons") or []}
        return ", ".join(sorted(reasons & BROKEN)) or None

    def commit(self) -> str:
        if self._commit is None:
            self._commit = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True,
                                          text=True, check=True).stdout.strip()
        return self._commit

    def provenance(self, tasks: list[str]) -> dict:
        return {"task_packages_commit": self.commit(),
                "agent": {key: value for key, value in self.agent.items() if key != "secret_env"},
                "tasks": {task: {"package_root": self.index[task]["package_root"], **self.resources(task),
                                 "agent_timeout_seconds": self.default_time_limit_seconds(task),
                                 "exact": self.exact(task), "pairs_with": self.pairs_with(task)} for task in tasks}}

    # -- jobs -------------------------------------------------------------------------
    def deploy(self, task: str) -> str:
        """Copy the task package to the cluster, without the working tree and the reference solutions."""
        target = f"{self.root}/deploy/{self.commit()}/{self.index[task]['package_root']}"
        if target not in self._deployed:
            self.ssh(f"mkdir -p {shlex.quote(target)}")
            self.push(self.package(task), target, ("environment/repo", "solution", "oracle.patch", "negative.patch",
                                                   "__pycache__"))
            self._deployed.add(target)
        return target

    def pairing(self, task: str, inputs: Path) -> dict | None:
        """What the job times its tree against. The reference travels with the job's inputs, which no agent
        container sees."""
        if not self.can_pair(task):
            return None
        settings = {"blocks": 4, "budget_seconds": 240, **(self.options.get("pairing") or {}),
                    "wallclock": bool(self.options.get("wallclock", False))}
        oracle = self.oracle(task)
        handed = next(iter(sorted(inputs.glob("reference.*"))), None)   # the framework's reference node, if not the baseline
        if oracle:
            shutil.copyfile(oracle, inputs / "oracle.patch")
            return {**settings, "reference": "oracle", "patch": "oracle.patch"}
        if handed:
            return {**settings, "reference": "first valid session", "patch": handed.name}
        return {**settings, "reference": "baseline", "patch": None}

    def submit(self, task: str, name: str, kind: str, directory: Path, avoid=(), **request) -> dict:
        resources, toml = self.resources(task), self.toml(task)
        inputs, remote = directory / "input", self.remote_dir(task, name)
        inputs.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(JOB, inputs / "job.py")
        shutil.copyfile(measure.__file__, inputs / "measure.py")   # the job computes what the scheduler would
        verify_seconds = int(toml.get("verifier", {}).get("timeout_sec", 3600))
        pairing = self.pairing(task, inputs)
        request = {"kind": kind, "task": task, "package": self.deploy(task), "image_root": self.options["image_root"],
                   "image": (self.options.get("images") or {}).get(task), "gpus": resources["gpus"],
                   "writable": ["/app/repo", "/app/build", "/app/.timer"], "verify_timeout_seconds": verify_seconds,
                   "agent": self.agent, "archive_location": ARCHIVE, "time_limit_seconds": 0, "pairing": pairing,
                   "reap": bool(self.options.get("reap_processes", False)), **request}
        (inputs / "request.json").write_text(json.dumps(request, indent=1, sort_keys=True) + "\n")
        limit = request["time_limit_seconds"] + verify_seconds + float(self.options.get("margin_minutes", 30)) * 60 \
            + (float(pairing["budget_seconds"]) * 2 if pairing else 0)
        memory = resources["memory_mb"] + int(self.options.get("memory_headroom_mb", 8192))
        clauses = [f"({self.options['requirements']})"] if self.options.get("requirements") else []
        unwanted = list(dict.fromkeys([*(self.ledger.excluded() if self.ledger else []), *avoid]))
        clauses += [f'(TARGET.Machine != "{host}")' for host in unwanted]
        description = (
            f"executable = /bin/sh\narguments = \"-c 'cd {remote} && exec python3 in/job.py in out'\"\n"
            f"initialdir = {remote}\noutput = {remote}/condor.out\nerror = {remote}/condor.err\nlog = {remote}/condor.log\n"
            f"request_cpus = {resources['cpus']}\nrequest_memory = {memory}\nrequest_gpus = {resources['gpus']}\n"
            + (f"requirements = {' && '.join(clauses)}\n" if clauses else "")
            + f"periodic_remove = (JobStatus == 2) && (time() - EnteredCurrentStatus > {int(limit)})\nqueue\n")
        quoted = shlex.quote(remote)
        # a resubmitted job must not see what an earlier attempt left behind, and that attempt is kept beside it
        self.ssh(f"mkdir -p {quoted} && chmod 700 {quoted} && cd {quoted} && "
                 "if [ -n \"$(ls -A out 2>/dev/null)\" ]; then n=1; while [ -e attempt-$n ]; do n=$((n+1)); done; "
                 "mkdir attempt-$n && mv in out condor.* attempt-$n/ 2>/dev/null; fi; rm -rf in out && mkdir in out")
        self.push(inputs, f"{remote}/in")
        self.ssh(f"cat > {quoted}/condor.sub", stdin=description)
        command = self.options.get("submit_command", "condor_submit")
        cluster = self.ssh(f"cd {quoted} && {command} -terse condor.sub").strip().splitlines()[-1].split(".")[0]
        (directory / "raw").mkdir(parents=True, exist_ok=True)
        (directory / "raw" / "condor.sub").write_text(description)
        return {"cluster": cluster, "remote": remote, "task_packages_commit": self.commit(), "avoided": unwanted}

    def submit_baseline(self, task: str, session_dir: Path, events) -> dict:
        handle = self.submit(task, "s000", "baseline", session_dir)
        events.emit("job_submitted", "s000", **handle)
        return handle

    def submit_session(self, spec, session_dir: Path, events) -> dict:
        inputs = session_dir / "input"   # the framework already wrote prompt.md, archive.md and the start file
        start = next((path.name for path in inputs.glob("start.*")), None) if spec.start_artifact else None
        handle = self.submit(spec.task, spec.session_id, "session", session_dir, start=start,
                             archive="archive.md" if (inputs / "archive.md").is_file() else None,
                             time_limit_seconds=spec.time_limit_seconds)
        events.emit("job_submitted", spec.session_id, **handle)
        return handle

    def submit_rescore(self, task: str, name: str, artifact: bytes, job_dir: Path, events, avoid=()) -> dict:
        (job_dir / "input").mkdir(parents=True, exist_ok=True)
        (job_dir / "input" / "start.patch").write_bytes(artifact)
        # on another node than the earlier scorings of this work, where the pool has one
        handle = self.submit(task, name, "rescore", job_dir, avoid=tuple(avoid),
                             start="start.patch" if artifact.strip() else None)
        events.emit("job_submitted", name, **handle)
        return handle

    def poll(self, jobs, events) -> set[str]:
        """HTCondor's JobStatus: 1 idle, 2 running, 3 removed, 4 completed, 5 held."""
        wanted = " || ".join(f"ClusterId == {int(job.handle['cluster'])}" for job in jobs)
        status = {}
        for query in ("condor_history", "condor_q"):   # the queue last: a job still in it is not over
            limit = f" -limit {len(jobs)}" if query == "condor_history" else ""
            for line in self.ssh(f"{query} -constraint {shlex.quote(wanted)}{limit} -af ClusterId JobStatus").splitlines():
                if len(line.split()) == 2:
                    status[line.split()[0]] = line.split()[1]
        finished = set()
        for job in jobs:
            state = status.get(job.handle["cluster"])   # a job the pool did not report on is not known to be over
            log = events.for_task(job.task)
            if state == "5":
                # held, and it would keep its slot forever. A job can be held after it finished its work (a
                # memory report at exit): then its result is there and is collected like any other.
                reason = self.ssh(f"condor_q {int(job.handle['cluster'])} -af HoldReason").strip()
                self.ssh(f"condor_rm {int(job.handle['cluster'])}")
                log.emit("job_held", job.session_id, cluster=job.handle["cluster"], reason=reason)
                state = "3"
            if state in ("3", "4"):
                finished.add(job.key)
                log.emit("job_finished", job.session_id, cluster=job.handle["cluster"], status=state)
        return finished

    @staticmethod
    def set_aside(session_dir: Path) -> Path:
        """Move a failed attempt's raw outputs to attempts/attempt-N: the session is resubmitted with the same
        inputs and must not find them, and they are kept for whoever wants to know what went wrong."""
        number = 1
        while (session_dir / "attempts" / f"attempt-{number}").exists():
            number += 1
        target = session_dir / "attempts" / f"attempt-{number}"
        target.mkdir(parents=True)
        if (session_dir / "raw").exists():
            shutil.move(str(session_dir / "raw"), str(target / "raw"))
        return target

    def collect(self, job, session_dir: Path, events) -> SessionResult:
        raw, remote = session_dir / "raw", job.handle["remote"]
        self.pull(f"{remote}/out", raw)
        for name in ("condor.out", "condor.err", "condor.log"):
            subprocess.run(["rsync", "-a", f"{self.host}:{remote}/{name}", str(raw / name)], capture_output=True)
        timing = self.timing(job.handle["cluster"])
        events.emit("collected", job.session_id, where=timing.get("where"))
        result = json.loads((raw / "result.json").read_text()) if (raw / "result.json").is_file() else {}
        verdict = result.get("verdict")
        if verdict is None:
            detail = result.get("error") or ""
            if not detail and (raw / "condor.err").is_file():
                detail = (raw / "condor.err").read_text(errors="replace")[-1500:]
            kept = self.set_aside(session_dir)
            raise JobFailed(f"{job.kind} job {job.handle['cluster']} produced no verdict (outputs kept in {kept})\n{detail}")
        phases = result.get("phases") or {}
        timing.update(phases=phases, agent_seconds=phases.get("agent"), verifier_seconds=phases.get("verify"),
                      pairing_seconds=phases.get("pair"), cpu_model=result.get("cpu_model"))
        gates = verdict.get("gates") if isinstance(verdict.get("gates"), dict) else {}
        reasons = [str(reason) for reason in verdict.get("hard_fail_reasons") or []]
        checks = {"correctness_ok": gates.get("correctness_ok") is True,
                  "scope_ok": gates.get("scope_ok", True) is True,
                  "import_origin_ok": gates.get("import_origin_ok", True) is True,
                  "no_hard_fail": not [reason for reason in reasons if not NOT_FASTER.match(reason)]}
        row, again = self.timing_row(
            checks=checks, speedup=verdict.get("speedup"), pairing=result.get("pairing"), reasons=reasons,
            extra={"reward": verdict.get("reward"), "ref_speedup": verdict.get("ref_speedup"),
                   "image": result.get("image"), "verifier_exit_code": result.get("verifier_exit_code")})
        again = again or policy.same_hardware(self.options.get("cpu_model"), result.get("cpu_model"))
        self.judge_machine(job, session_dir, timing.get("where"), again)
        patch = (raw / "final.patch").read_bytes() if (raw / "final.patch").is_file() else b""
        if job.kind != "session":
            return SessionResult(scores=[row], artifacts={"final": patch}, timing=timing, remeasure=again)
        transcript = raw / "agent.jsonl"
        refused = transcripts.refusal(transcript)
        if refused:
            kept = self.set_aside(session_dir)
            raise AgentUnavailable(f"the agent's service refused the session: {refused[0]} (outputs kept in {kept})",
                                   refused[1])
        calls = transcripts.tool_calls(transcript)
        flags = {}
        for flag, pattern in (self.options.get("forbidden") or {}).items():
            hits = [f"{name}: {text[:200]}" for name, text in calls if re.search(pattern, text)]
            if hits:
                flags[flag] = f"{len(hits)} tool call(s) matched the campaign's `{flag}` rule; first: {hits[0]}"
        return SessionResult(
            scores=[row], artifacts={"final": patch}, report=extract_report(transcripts.texts(transcript)), timing=timing,
            usage=transcripts.usage(transcript), flags=flags, remeasure=again,
            stats={"agent": result.get("agent"), "tool_calls": len(calls), "ended_processes": result.get("ended_processes"),
                   "by_tool": dict(Counter(name for name, _ in calls).most_common())})

    def judge_machine(self, job, job_dir: Path, where: str | None, distrusted: str | None) -> None:
        """With a ledger: note, beside a measurement that is not trusted, where it was taken; and when a repeat
        scoring elsewhere is trusted, give the machine of the distrusted one a strike."""
        if self.ledger is None:
            return
        note = job_dir / "raw" / "distrusted.json"
        if distrusted and where:
            note.write_text(json.dumps({"where": where, "reason": distrusted}))
        if job.kind != "confirm" or distrusted:
            return
        session = job_dir.parent.parent                      # sessions/<sid>/confirm/r<n> -> sessions/<sid>
        earlier = [session / "raw" / "distrusted.json", *sorted(session.glob("confirm/r*/raw/distrusted.json"))]
        for path in (path for path in earlier if path.is_file()):
            suspect = json.loads(path.read_text())
            if suspect["where"] != where:
                self.ledger.strike(suspect["where"], suspect["reason"],
                                   {"campaign": self.campaign, "task": job.task, "session": session.name})

    def timing(self, cluster: str) -> dict:
        """Queue and run time of a finished job, from its history ClassAd."""
        try:
            fields = self.ssh(f"condor_history {int(cluster)} -limit 1 -af QDate JobCurrentStartDate "
                              "CompletionDate EnteredCurrentStatus LastRemoteHost").split()
            queued, started, completed, entered = (float(value) if value.isdigit() else 0.0 for value in fields[:4])
            finished = completed or entered
            return {"queue_seconds": started - queued if started and queued else None,
                    "job_seconds": finished - started if finished and started else None,
                    "where": fields[4].split("@")[-1] if len(fields) > 4 else None}
        except (subprocess.CalledProcessError, ValueError):
            return {}
