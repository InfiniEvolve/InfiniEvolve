"""Evaluate DepthDash submissions, including kernels and equivalent fast models."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys
import time

FROZEN = ("train_gpt.py", "model.py", "tracks.py", "check_equivalence.py")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_workspace(workspace, manifest):
    # Only the four files frozen by DepthDash belong in the prepared manifest.
    # Helpers, kernels, build scripts, tests and fast models are legitimate artifacts.
    for name, expected in manifest.items():
        path = workspace / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError(f"frozen file changed: {name}")


def entry_path(workspace, name):
    if not isinstance(name, str) or Path(name).is_absolute():
        raise ValueError("entry paths must be relative to the workspace")
    path = workspace / name
    if not path.resolve().is_relative_to(workspace.resolve()) or not path.is_file():
        raise ValueError(f"missing or external entry file: {name}")
    return name


def build_plan(workspace, config):
    options = json.loads((workspace / "submission/run.json").read_text())
    allowed = {"residual", "model", "steps", "lr", "microbatch", "checkpoint", "compile", "val_every"}
    if not isinstance(options, dict) or set(options) - allowed:
        raise ValueError("run.json supports only residual, model, steps, lr, microbatch, checkpoint, compile, val_every")
    residual = entry_path(workspace, options.get("residual", "submission/residual.py"))
    model = entry_path(workspace, options["model"]) if options.get("model") else None
    gpus, seeds, track = config["gpus"], config["seeds"], config["track"]
    if type(gpus) is not int or gpus not in (1, 2, 4, 8):
        raise ValueError("DepthDash supports 1, 2, 4 or 8 GPUs")
    if not seeds or any(type(seed) is not int or seed < 0 for seed in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError("provide a nonempty, unique list of fixed nonnegative seeds")
    if config["score"] not in ("diagnostic-loss", "time-to-target"):
        raise ValueError("unknown scoring mode")
    budget = config.get("budget_seconds")
    if config["score"] == "diagnostic-loss":
        if budget is None or not math.isfinite(budget) or budget <= 0:
            raise ValueError("diagnostic-loss requires an explicit positive --budget")
        if "steps" in options:
            raise ValueError("DepthDash --steps and --budget are exclusive; remove steps for budget diagnostics")
    else:
        if budget is not None or gpus != 8 or len(seeds) < 2:
            raise ValueError("time-to-target requires 8 GPUs, multiple fixed seeds and no time budget")
        target = config.get("target")
        if target is None or not math.isfinite(target) or target <= 0:
            raise ValueError("the upstream track has no calibrated target; do not invent one")
        if "steps" not in options:
            raise ValueError("set steps in submission/run.json before a time-to-target run")
    for key in ("steps", "microbatch", "val_every"):
        if key in options and (type(options[key]) is not int or options[key] < (0 if key == "val_every" else 1)):
            raise ValueError(f"invalid {key}")
    if "microbatch" in options and (512 // gpus) % options["microbatch"]:
        raise ValueError("microbatch must divide 512 / GPU count")
    if "lr" in options and (type(options["lr"]) not in (int, float) or not math.isfinite(options["lr"]) or options["lr"] <= 0):
        raise ValueError("lr must be positive and finite")
    if options.get("compile", "auto") not in ("auto", "model", "sublayer", "none"):
        raise ValueError("invalid compile mode")
    if type(options.get("checkpoint", False)) is not bool:
        raise ValueError("checkpoint must be a boolean")
    common = ["--track", track, "--residual", residual]
    if model:
        common += ["--model", model]
    equivalence = [sys.executable, "check_equivalence.py", *common] if model else None
    command = [sys.executable, "-m", "torch.distributed.run", "--standalone",
               f"--nproc_per_node={gpus}", "train_gpt.py", *common]
    if budget is not None:
        command += ["--budget", str(budget)]
    for key in ("steps", "lr", "microbatch", "compile", "val_every"):
        if key in options:
            command += ["--" + key.replace("_", "-"), str(options[key])]
    if options.get("checkpoint"):
        command += ["--checkpoint"]
    return {"options": options, "equivalence": equivalence,
            "training": [command + ["--trials", "1", "--seed", str(seed)] for seed in seeds]}


def parse_result(text, track, seed, budget, steps=None):
    lines = [line for line in text.splitlines() if line.startswith("result track:")]
    if len(lines) != 1:
        raise ValueError(f"expected one final result, got {len(lines)}")
    pairs = [part.split(":", 1) for part in lines[0].split()[1:]]
    row = dict(pairs)
    # Upstream prints actual steps followed by the configured step limit. Check both.
    counts = [int(value) for key, value in pairs if key == "steps"]
    if not counts or len(set(counts)) != 1:
        raise ValueError("incomplete training: actual and configured steps differ")
    if row["track"] != track or row["seed"] != str(seed) or row["trial"] != "0":
        raise ValueError("result does not match the requested track/seed/trial")
    if budget is not None:
        if row["record_run"] != "False" or row.get("budget") != f"{budget:.0f}s":
            raise ValueError("expected a fixed-time diagnostic, not a record run")
    elif row["record_run"] != "True" or "budget" in row or counts[0] != steps:
        raise ValueError("expected the declared number of training steps")
    loss = float(row["val_loss"])
    seconds = float(row["train_time"].removesuffix("s"))
    memory = float(row["peak_mem"].removesuffix("GiB"))
    if not all(math.isfinite(v) for v in (loss, seconds, memory)) or loss <= 0 or seconds <= 0 or counts[0] <= 0:
        raise ValueError("nonfinite or incomplete training result")
    if budget is not None and seconds < budget:
        raise ValueError("incomplete training budget")
    return {"val_loss": loss, "train_seconds": seconds, "steps": counts[0], "peak_memory_gib": memory}


def summarize(rows, config):
    metrics = {key: statistics.mean(row[key] for row in rows)
               for key in ("val_loss", "train_seconds", "steps", "peak_memory_gib")}
    if config["score"] == "time-to-target":
        from scipy.stats import ttest_1samp
        losses, target = [row["val_loss"] for row in rows], config["target"]
        if len(set(losses)) == 1:
            # Limiting one-sided t-test at zero sample variance; equality is not significance.
            pvalue = 0.0 if losses[0] < target else 1.0
        else:
            pvalue = float(ttest_1samp(losses, target, alternative="less").pvalue)
        metrics["target_pvalue"] = pvalue
        return metrics, math.isfinite(pvalue) and pvalue < 0.01 and metrics["val_loss"] <= target
    return metrics, True


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
            raise ValueError(f"process timed out after {timeout:.0f}s; see {output}")
    if result != 0:
        raise ValueError(f"process exited {result}; see {output}")


def evaluate(task, workspace, output):
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads((task / "evaluation.json").read_text())
    manifest = json.loads((task / "frozen.json").read_text())
    checks = {"frozen_sources": False, "equivalence": False, "completed": False, "target": False}
    metrics, reasons, rows = {}, [], []
    started = time.monotonic()
    data_link, created_link = workspace / "data/fineweb10B", False
    try:
        verify_workspace(workspace, manifest)
        checks["frozen_sources"] = True
        plan = build_plan(workspace, config)
        (output / "invocation.json").write_text(json.dumps({"plan": plan, "config": config, "frozen": manifest}, indent=2))
        if config["score"] == "time-to-target":
            import scipy.stats  # fail before expensive GPU work if the optional dependency is missing
        data = Path(config["data_dir"]).resolve()
        if not list(data.glob("fineweb_train_*.bin")) or not list(data.glob("fineweb_val_*.bin")):
            raise ValueError("FineWeb data missing")
        data_link.parent.mkdir(parents=True, exist_ok=True)
        if data_link.exists() or data_link.is_symlink():
            if data_link.resolve() != data:
                raise ValueError("workspace data path differs from configured dataset")
        else:
            data_link.symlink_to(data, target_is_directory=True)
            created_link = True

        def run(command, name):
            remaining = config["timeout_seconds"] - (time.monotonic() - started)
            if remaining <= 0:
                raise ValueError("evaluation wall-time limit reached")
            run_logged(command, workspace, output / name, remaining)
            verify_workspace(workspace, manifest)

        if plan["equivalence"]:
            run(plan["equivalence"], "equivalence.log")
            expected = (f"equivalence track:{config['track']} residual:{plan['options'].get('residual', 'submission/residual.py')} "
                        f"model:{plan['options']['model']}: PASS")
            if expected not in (output / "equivalence.log").read_text().splitlines():
                raise ValueError("upstream equivalence PASS missing")
        checks["equivalence"] = True  # no fast model needs no fast/reference comparison
        for seed, command in zip(config["seeds"], plan["training"]):
            log_name = f"train-seed-{seed}.log"
            run(command, log_name)
            row = parse_result((output / log_name).read_text(), config["track"], seed,
                               config.get("budget_seconds"), plan["options"].get("steps"))
            rows.append({"seed": seed, **row})
            (output / "seeds.json").write_text(json.dumps(rows, indent=2))
        metrics, checks["target"] = summarize(rows, config)
        checks["completed"] = True
        if not checks["target"]:
            reasons.append("mean final loss has not met the upstream target at one-sided t-test p < 0.01")
    except (ValueError, OSError, KeyError, TypeError, ImportError) as exc:
        reasons.append(str(exc))
    finally:
        try:
            verify_workspace(workspace, manifest)
        except (ValueError, OSError) as exc:
            checks["frozen_sources"] = False
            reasons.append(str(exc))
        if created_link and data_link.is_symlink():
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
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="only print commands; no model import, GPU work or data access")
    args = parser.parse_args()
    if args.dry_run:
        print(json.dumps(build_plan(args.workspace.resolve(), json.loads((args.task / "evaluation.json").read_text())), indent=2))
    else:
        if args.output is None:
            parser.error("--output is required unless --dry-run")
        print(json.dumps(evaluate(args.task.resolve(), args.workspace.resolve(), args.output.resolve()), allow_nan=False))
