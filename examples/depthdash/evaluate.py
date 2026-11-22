"""Evaluate a frozen DepthDash workspace. Outputs an InfiniEvolve JSON score last."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_workspace(workspace, manifest):
    for name, expected in manifest.items():
        path = workspace / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError(f"frozen file changed: {name}")
    allowed = set(manifest) | {"submission/residual.py"}
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace)
        if any(part in (".git", "__pycache__") for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"symlink not allowed in candidate: {relative}")
        if path.is_file() and relative.as_posix() not in allowed:
            raise ValueError(f"only submission/residual.py may change: {relative}")
    if not (workspace / "submission/residual.py").is_file():
        raise ValueError("missing submission/residual.py")


def parse_result(text, track, seed, budget):
    lines = [line for line in text.splitlines() if line.startswith("result track:")]
    if len(lines) != 1:
        raise ValueError(f"expected one final result, got {len(lines)}")
    row = dict(part.split(":", 1) for part in lines[0].split()[1:])
    if row["track"] != track or row["seed"] != str(seed) or row["trial"] != "0":
        raise ValueError("result does not match the requested track/seed/trial")
    if row["record_run"] != "False" or row.get("budget") != f"{budget:.0f}s":
        raise ValueError("expected a fixed-time diagnostic, not a record run")
    loss = float(row["val_loss"])
    seconds = float(row["train_time"].removesuffix("s"))
    steps = int(row["steps"])
    memory = float(row["peak_mem"].removesuffix("GiB"))
    if not all(math.isfinite(v) for v in (loss, seconds, memory)) or loss <= 0 or seconds < budget or steps <= 0:
        raise ValueError("nonfinite or incomplete training result")
    return {"val_loss": loss, "train_seconds": seconds, "steps": steps, "peak_memory_gib": memory}


def run_logged(command, cwd, output, timeout):
    with output.open("w") as log:
        process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            result = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise ValueError(f"process timed out after {timeout}s; see {output}")
    if result != 0:
        raise ValueError(f"process exited {result}; see {output}")


def evaluate(task, workspace, output):
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads((task / "evaluation.json").read_text())
    manifest = json.loads((task / "frozen.json").read_text())
    checks, metrics, reasons = {}, {}, []
    started = time.monotonic()
    data_link = workspace / "data/fineweb10B"
    try:
        verify_workspace(workspace, manifest)
        checks["frozen_sources"] = True
        data = Path(config["data_dir"])
        if not list(data.glob("fineweb_train_*.bin")) or not list(data.glob("fineweb_val_*.bin")):
            raise ValueError("FineWeb data missing")
        run_logged([sys.executable, str(task / "check_contract.py"), str(workspace)], workspace,
                   output / "contract.log", 90)
        checks["contract_smoke"] = True
        data_link.symlink_to(data.resolve(), target_is_directory=True)
        command = [sys.executable, "-m", "torch.distributed.run", "--standalone", "--nproc_per_node=1",
                   "train_gpt.py", "--track", "deep", "--residual", "submission/residual.py",
                   "--budget", str(config["budget_seconds"]), "--lr", "0.025", "--seed", str(config["seed"]),
                   "--microbatch", "8", "--compile", "sublayer", "--compile-warmup", "20"]
        (output / "invocation.json").write_text(json.dumps({"command": command, "config": config,
            "candidate_sha256": digest(workspace / "submission/residual.py"), "frozen": manifest}, indent=2))
        run_logged(command, workspace, output / "train.log", config["timeout_seconds"])
        metrics = parse_result((output / "train.log").read_text(), "deep", config["seed"], config["budget_seconds"])
        checks["completed"] = True
        # Recheck immutable files after executing untrusted candidate code.
        for name, expected in manifest.items():
            if digest(workspace / name) != expected:
                checks["frozen_sources"] = False
                raise ValueError(f"frozen source changed during evaluation: {name}")
    except (ValueError, OSError, KeyError) as exc:
        checks["completed"] = False
        reasons.append(str(exc))
    finally:
        if data_link.is_symlink():
            data_link.unlink()
        if (workspace / "logs").is_dir():
            shutil.copytree(workspace / "logs", output / "upstream_logs", dirs_exist_ok=True)
    metrics["evaluation_wall_seconds"] = time.monotonic() - started
    result = {"checks": checks, "metrics": metrics, "reasons": reasons}
    (output / "score.json").write_text(json.dumps(result, indent=2, allow_nan=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=Path, default=Path(os.environ.get("EVOLVE_TASK", ".")))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.task.resolve(), args.workspace.resolve(), args.output.resolve()), allow_nan=False))
