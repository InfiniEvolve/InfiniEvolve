#!/usr/bin/env python3
"""One fai-bench job on a cluster node: score the baseline, run one agent session, or score archived work.

    python3 job.py <in directory> <out directory>

Follows the task package's own flow, with the task's prebuilt Apptainer image:
    copy the writable directories (/app/repo, ...) out of the image -> apply the work the session starts
    from -> the agent, inside the image -> capture what changed as a patch against the baseline
    -> pre_artifacts.sh -> the package's tests/test.sh on that tree -> /logs/verifier/reward.json
    -> where asked and the tree is correct: time it in alternation with a reference tree (measure.py)

<in>/request.json says what to do; <out>/result.json, written last, says what happened. Everything else in
<out> is kept as the session's raw record. Standard library only, plus measure.py, which the adapter
sends along in <in>: this file runs on the node as it is.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

PROXY_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")


class Job:
    def __init__(self, inputs: Path, out: Path) -> None:
        self.inputs, self.out = inputs, out
        self.request = json.loads((inputs / "request.json").read_text())
        self.tag = measure.tag_job()      # every process of this job carries it, inside containers too
        self.ended: dict = {}
        self.scratch = Path(tempfile.mkdtemp(prefix="infinievolve-", dir=os.environ.get("_CONDOR_SCRATCH_DIR", "/tmp")))
        self.image = self.find_image()
        self.logs = self.scratch / "logs"
        self.phases: dict = {}
        for name in ("verifier", "artifacts", "agent"):
            (self.logs / name).mkdir(parents=True)

    # -- the container -----------------------------------------------------------------
    def find_image(self) -> str:
        if self.request.get("image"):
            return self.request["image"]
        found = sorted(Path(self.request["image_root"]).glob(f"{self.request['task']}-*.sif"),
                       key=lambda path: path.stat().st_mtime)
        if not found:
            raise RuntimeError(f"no image {self.request['task']}-*.sif in {self.request['image_root']}")
        return str(found[-1])

    def home(self, name: str) -> Path:
        """A writable home for one container (caches and temporary files land there, not in the image)."""
        home = self.scratch / name
        (home / "tmp").mkdir(parents=True, exist_ok=True)
        return home

    def environment(self, extra: dict | None = None) -> dict:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "APPTAINERENV_TMPDIR": "/job-home/tmp",
               "APPTAINERENV_XDG_CACHE_HOME": "/job-home/cache", measure.JOB_TAG: self.tag,
               "APPTAINERENV_" + measure.JOB_TAG: self.tag}
        if "LD_LIBRARY_PATH" in os.environ:   # --nv needs the host's driver libraries
            env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
        for key in PROXY_KEYS:
            if os.environ.get(key):
                env[key] = env["APPTAINERENV_" + key] = os.environ[key]
        env.update({"APPTAINERENV_" + key: str(value) for key, value in (extra or {}).items()})
        return env

    def container(self, binds: list, pwd: str = "/app/repo") -> list:
        command = ["apptainer", "exec", "--cleanenv", "--contain", "--no-home", "--pwd", pwd]
        if self.request.get("gpus"):
            command.append("--nv")
        for host, target in binds:
            command += ["--bind", f"{host}:{target}"]
        return command + [self.image]

    def tree(self, root: str) -> Path:
        return self.scratch / "roots" / root.strip("/").replace("/", "__")

    def copy_out(self, root: str, target: Path) -> None:
        """Copy a directory out of the image; one the image does not have starts empty."""
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        script = f'if [ -d "{root}" ]; then tar -C "{root}" -cf - .; else tar -cf - -T /dev/null; fi'
        reader = subprocess.Popen(["apptainer", "exec", "--cleanenv", self.image, "sh", "-c", script],
                                  stdout=subprocess.PIPE)
        subprocess.run(["tar", "-C", str(target), "-xf", "-"], stdin=reader.stdout, timeout=1800)
        if reader.wait(timeout=60) != 0:
            raise RuntimeError(f"could not copy {root} out of {self.image}")

    def binds(self) -> list:
        return [(self.tree(root), root) for root in self.request["writable"]] + [(self.logs, "/logs")]

    def timed(self, name: str, function, *args):
        began = time.time()
        try:
            return function(*args)
        finally:
            self.phases[name] = round(time.time() - began, 1)

    # -- the steps ---------------------------------------------------------------------
    def prepare(self) -> None:
        for root in self.request["writable"]:
            self.copy_out(root, self.tree(root))
        start = self.request.get("start")
        if start:
            subprocess.run(["git", "-C", str(self.tree("/app/repo")), "apply", "--binary", "--whitespace=nowarn",
                            str(self.inputs / start)], check=True, capture_output=True)

    def agent(self) -> dict:
        request, agent = self.request, self.request["agent"]
        home = self.home("agent-home")
        binds = self.binds() + [(home, "/job-home"), (agent["path"], "/opt/agent/claude:ro")]
        if request.get("archive"):
            binds.append((self.inputs / request["archive"], request["archive_location"] + ":ro"))
        token = next(line.split("=", 1)[1].strip().strip("'\"") for line in Path(agent["secret_env"]).read_text().splitlines()
                     if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="))
        env = self.environment({"CLAUDE_CODE_OAUTH_TOKEN": token, "IS_SANDBOX": "1"})
        command = ["timeout", "--signal=TERM", "--kill-after=30s", f"{int(request['time_limit_seconds'])}s",
                   *self.container(binds), "env", "HOME=/job-home", "/opt/agent/claude", "-p",
                   "--model", agent["model"], "--effort", agent["effort"], "--dangerously-skip-permissions",
                   "--input-format", "text", "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
        with (self.inputs / "prompt.md").open("rb") as prompt, (self.out / "agent.jsonl").open("wb") as transcript, \
                (self.out / "agent.err").open("wb") as errors:
            # its own process group, so whatever the agent left running can be ended with it
            process = subprocess.Popen(command, env=env, stdin=prompt, stdout=transcript, stderr=errors,
                                       start_new_session=True)
            code = process.wait()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return {"return_code": code, "deadline_stop": code in (124, 137)}

    def capture(self) -> bytes:
        """Everything that differs from the baseline commit of /app/repo, new files included."""
        repo = str(self.tree("/app/repo"))
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True, capture_output=True)
        patch = subprocess.run(["git", "-C", repo, "diff", "--cached", "--binary", "HEAD"], check=True,
                               capture_output=True).stdout
        subprocess.run(["git", "-C", repo, "reset", "-q"], check=True, capture_output=True)   # leave the tree as it was
        return patch

    def pre_artifacts(self) -> None:
        command = self.container(self.binds() + [(self.request["package"], "/opt/task-package:ro"),
                                                 (self.home("artifacts-home"), "/job-home")]) + \
            ["bash", "/opt/task-package/pre_artifacts.sh"]
        with (self.out / "pre_artifacts.out").open("wb") as stream:
            subprocess.run(command, env=self.environment(), stdout=stream, stderr=subprocess.STDOUT, timeout=1800)

    def verify(self) -> int:
        binds = self.binds() + [(Path(self.request["package"]) / "tests", "/tests:ro"),
                                (self.home("verify-home"), "/job-home")]
        command = ["timeout", "--signal=TERM", "--kill-after=60s", f"{int(self.request['verify_timeout_seconds'])}s",
                   *self.container(binds), "bash", "/tests/test.sh"]
        with (self.out / "verify.out").open("wb") as stream:
            return subprocess.run(command, env=self.environment(), stdout=stream, stderr=subprocess.STDOUT).returncode

    def quiet(self, stage: str) -> None:
        """End what this job's earlier steps left running, where the campaign asked for that (off by default)."""
        if self.request.get("reap"):
            self.ended[stage] = measure.reap_tagged(self.tag)

    # -- timing against a reference ----------------------------------------------------
    def time_tree(self, tree: Path, log: Path, timeout: float) -> float:
        """One run of the task's timing workload on a tree, in a fresh container; its timing in milliseconds."""
        binds = [(tree, "/app/repo"), (Path(self.request["package"]) / "tests", "/tests:ro"),
                 (self.home("timing-home"), "/job-home")]
        # only where the campaign asks for it: by default the workload's own clock stands
        program = ["python3", "-c", measure.WALLCLOCK] if self.request["pairing"].get("wallclock") else ["python3"]
        try:
            done = subprocess.run(self.container(binds, pwd="/tests") + program + ["/tests/workload.py", "timing"],
                                  env=self.environment(), capture_output=True, text=True, timeout=max(1, timeout))
        finally:
            self.quiet("timing")   # whatever one arm left running must not load the next
        log.write_text(done.stdout + "\n--- stderr ---\n" + done.stderr)
        for line in reversed((done.stdout + "\n" + done.stderr).splitlines()):   # "<TOKEN> {"timing_ms": ...}"
            start = line.find("{")
            if start >= 0 and "timing_ms" in line:
                try:
                    value = float(json.loads(line[start:])["timing_ms"])
                except (ValueError, KeyError, TypeError):
                    continue
                if value > 0:
                    return value
        raise RuntimeError(f"the timing workload reported no timing (exit {done.returncode})")

    def pair(self) -> dict:
        """The verified tree against the reference tree, in alternation on this node."""
        settings = self.request["pairing"]
        root = self.scratch / "pair"
        logs = self.out / "pairing"
        logs.mkdir(parents=True, exist_ok=True)
        candidate = measure.snapshot(self.tree("/app/repo"), root / "candidate")   # never the live tree
        before = measure.digest(candidate)
        reference = root / "reference"
        self.copy_out("/app/repo", reference)                                      # the image's own, untouched
        if settings.get("patch"):
            subprocess.run(["git", "-C", str(reference), "apply", "--binary", "--whitespace=nowarn",
                            str(self.inputs / settings["patch"])], check=True, capture_output=True)
        deadline, count = time.time() + float(settings.get("budget_seconds", 240)), {"n": 0}

        def arm(name: str, tree: Path):
            def run() -> float:
                count["n"] += 1
                return self.time_tree(tree, logs / f"{count['n']:02d}-{name}.log", deadline - time.time() + 120)
            return run

        record = {"reference": settings["reference"], "blocks_wanted": int(settings.get("blocks", 4))}
        try:
            blocks = measure.run_blocks(arm("candidate", candidate), arm("reference", reference),
                                        blocks=record["blocks_wanted"],
                                        budget_seconds=float(settings.get("budget_seconds", 240)))
            record.update(measure.paired(blocks), raw=blocks, status="complete")
        except (RuntimeError, subprocess.SubprocessError) as error:
            record.update(status=f"failed: {str(error)[:300]}")
        if measure.digest(candidate) != before:
            record["status"] = "candidate_changed"       # what was timed did not stay what it was
        return record

    def run(self) -> dict:
        result = {"kind": self.request["kind"], "task": self.request["task"], "image": self.image,
                  "host": os.uname().nodename, "cpu_model": cpu_model()}
        self.timed("prepare", self.prepare)
        if self.request["kind"] == "session":
            result["agent"] = self.timed("agent", self.agent)
            self.quiet("agent")
        (self.out / "final.patch").write_bytes(self.capture())
        self.timed("pre_artifacts", self.pre_artifacts)
        result["verifier_exit_code"] = self.timed("verify", self.verify)
        self.quiet("verify")
        shutil.copytree(self.logs, self.out / "logs", dirs_exist_ok=True)
        reward = self.logs / "verifier" / "reward.json"
        verdict = result["verdict"] = json.loads(reward.read_text()) if reward.is_file() else None
        correct = bool(verdict) and (verdict.get("gates") or {}).get("correctness_ok") is True
        if self.request.get("pairing") and correct:
            result["pairing"] = self.timed("pair", self.pair)
        result["ended_processes"] = self.ended
        return result


def cpu_model() -> str | None:
    try:
        return next(line.split(":", 1)[1].strip() for line in open("/proc/cpuinfo") if line.startswith("model name"))
    except (OSError, StopIteration):
        return None


def main() -> int:
    inputs, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(inputs))
    global measure
    import measure   # sent along by the adapter
    out.mkdir(parents=True, exist_ok=True)
    job, result = None, {}
    try:
        job = Job(inputs, out)
        result = job.run()
    except Exception as error:   # whatever broke, say so in the result rather than leave none
        import traceback
        result["error"] = f"{type(error).__name__}: {error}"
        (out / "job_error.txt").write_text(traceback.format_exc())
    finally:
        result["phases"] = job.phases if job else {}
        temporary = out / "result.json.tmp"
        temporary.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
        temporary.replace(out / "result.json")   # last: its presence says the job is over
        if job:
            shutil.rmtree(job.scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
