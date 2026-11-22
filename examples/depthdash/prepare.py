"""Prepare an isolated fixed-time pilot from an exact DepthDash checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import yaml

COMMIT = "96d683cf3284522e22696d9e2de135ff0859c2fd"
HERE = Path(__file__).resolve().parent


def prepare(upstream, task, data_dir, budget=600, seed=0):
    actual = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if actual != COMMIT:
        raise ValueError(f"expected DepthDash {COMMIT}, got {actual}")
    if subprocess.check_output(["git", "-C", str(upstream), "status", "--porcelain"], text=True).strip():
        raise ValueError("upstream checkout must be clean")
    if budget < 1:
        raise ValueError("budget must be positive")
    task.mkdir(parents=True, exist_ok=False)
    workspace = task / "workspace"
    frozen = {}
    names = subprocess.check_output(["git", "-C", str(upstream), "ls-files", "-z"]).decode().split("\0")
    for name in filter(None, names):
        src, dst = upstream / name, workspace / name
        if src.is_symlink():
            raise ValueError(f"upstream symlink not supported: {name}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        frozen[name] = hashlib.sha256(dst.read_bytes()).hexdigest()
    (workspace / "submission").mkdir()
    shutil.copy2(workspace / "baselines/preln.py", workspace / "submission/residual.py")
    for name in ("evaluate.py", "check_contract.py"):
        shutil.copy2(HERE / name, task / name)
    (task / "frozen.json").write_text(json.dumps(frozen, indent=2, sort_keys=True))
    (task / "evaluation.json").write_text(json.dumps({"upstream_commit": COMMIT, "data_dir": str(data_dir.resolve()),
        "budget_seconds": budget, "seed": seed, "timeout_seconds": 1500}, indent=2))
    spec = {
        "instruction": """Improve the residual connections of DepthDash's deep track (28 layers, width 512).
Only edit submission/residual.py. The starting residual is Pre-LN. Read README.md, model.py,
and baselines/ to understand the interface and other designs. Every other file is frozen.
Call each black-box sublayer exactly once, in order; operate independently per token.
Do not inspect sublayer internals except constant weight rescaling in init_, as allowed upstream.
Do not read validation data or change the training/evaluation, seed, data, backbone, or optimizer.
This pilot fixes Muon lr=0.025, a 600-second training budget, seed=0, microbatch=8,
sublayer compilation, and 20 warmup steps; the evaluator supplies their exact configured values.
It ranks final validation loss (lower is better), on a single H100; this is not an official record.
The final validation covers the original 10,485,760 tokens. Only one training evaluation is
performed after each agent session, including CPU contract checks. During the session use
the available file tools and reasoning. Use archived losses to guide a small, interpretable change.
In your report describe the change, why it might help, and any limitations. Do not claim results
you have not measured. Avoid data-dependent or seed-dependent special cases.""",
        "evaluate": ["{python}", "{task}/evaluate.py", "--task", "{task}", "--output", "../raw/depthdash"],
        "evaluate_timeout_seconds": 1620,
        "time_limit_seconds": 480,
        "declaration": {"metrics": {"val_loss": {"direction": "min", "scale": "absolute", "format": "{:.5f}"},
            "train_seconds": {"direction": "min"}, "steps": {"direction": "max"},
            "peak_memory_gib": {"direction": "min"}, "evaluation_wall_seconds": {"direction": "min"}},
            "rank_by": "val_loss", "show": ["val_loss", "train_seconds", "steps"],
            "gate": ["frozen_sources", "contract_smoke", "completed"], "artifact": "patch"},
        "prompt": {"feedback": "Read the archive for prior training results. CPU contract checks and "
            "training run automatically after the session; the agent has file tools only in this pilot."},
    }
    spec["instruction"] = spec["instruction"].replace("600-second", f"{budget}-second").replace("seed=0", f"seed={seed}")
    (task / "task.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    return task


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--budget", type=int, default=600)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    print(prepare(args.upstream.resolve(), args.task.resolve(), args.data_dir, args.budget, args.seed))
