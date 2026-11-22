"""Prepare an isolated DepthDash task with upstream's submission rules."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

from evaluate import FROZEN, build_plan

COMMIT = "96d683cf3284522e22696d9e2de135ff0859c2fd"
HERE = Path(__file__).resolve().parent


def prepare(upstream, task, data_dir, *, track="deep", gpus=8, seeds=(0, 1, 2),
            score="time-to-target", budget=None, steps=None, timeout=7200, session_seconds=None):
    actual = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if actual != COMMIT:
        raise ValueError(f"expected DepthDash {COMMIT}, got {actual}")
    if subprocess.check_output(["git", "-C", str(upstream), "status", "--porcelain"], text=True).strip():
        raise ValueError("upstream checkout must be clean")
    # Load only the frozen track declarations, without importing Torch or training code.
    spec = importlib.util.spec_from_file_location("depthdash_tracks", upstream / "tracks.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # Avoid writing __pycache__ into the clean upstream checkout.
    exec(compile((upstream / "tracks.py").read_text(), str(upstream / "tracks.py"), "exec"), module.__dict__)
    shape = module.TRACKS[track]
    if timeout <= 0 or (session_seconds is not None and session_seconds <= 0):
        raise ValueError("operational timeouts must be positive")
    if score == "time-to-target" and shape.target is None:
        raise ValueError(f"{track} has no calibrated upstream target at {COMMIT}; use explicit diagnostic-loss mode")
    config = {"upstream_commit": COMMIT, "data_dir": str(data_dir.resolve()), "track": track,
              "gpus": gpus, "seeds": list(seeds), "score": score, "target": shape.target,
              "budget_seconds": budget, "timeout_seconds": timeout}
    options = {"residual": "submission/residual.py"}
    if steps is not None:
        options["steps"] = steps
    elif score == "time-to-target" and shape.steps is not None:
        options["steps"] = shape.steps
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
        if name in FROZEN:
            frozen[name] = hashlib.sha256(dst.read_bytes()).hexdigest()
    if set(frozen) != set(FROZEN):
        raise ValueError("upstream is missing a frozen file")
    (workspace / "submission").mkdir()
    shutil.copy2(workspace / "baselines/preln.py", workspace / "submission/residual.py")
    (workspace / "submission/run.json").write_text(json.dumps(options, indent=2) + "\n")
    build_plan(workspace, config)  # validate configuration without starting any process
    shutil.copy2(HERE / "evaluate.py", task / "evaluate.py")
    (task / "frozen.json").write_text(json.dumps(frozen, indent=2, sort_keys=True))
    (task / "evaluation.json").write_text(json.dumps(config, indent=2))
    protocol = (f"This campaign is a diagnostic: rank mean final val_loss after {budget} timed seconds per seed. "
                "DepthDash --budget and --steps are exclusive. This is not a leaderboard record."
                if score == "diagnostic-loss" else
                f"Rank mean train_time, gated by mean final loss <= {shape.target} at one-sided t-test p < 0.01. "
                "Use the declared steps on all seeds; the allocation must be 8 H100 SXM GPUs.")
    instruction = f"""Optimize DepthDash's {track} track (L{shape.num_layers}, d{shape.model_dim}), starting from Pre-LN.
Read README.md, model.py, check_equivalence.py, and baselines/ (including baselines/fast/).
Follow the upstream Rules section. The four frozen files are train_gpt.py, model.py, tracks.py,
and check_equivalence.py. Other source files may be added or edited, including helpers, custom
Triton/CUDA/C++ operators, build scripts and tests. Keep submission entry files and their helpers
in the same directory so upstream records their sources. Do not modify the external task/evaluator.

You have Bash as well as file tools. You may write operators, invoke the compiler, run GPU tests,
profile, and run multiple development training/equivalence checks during a session. No CPU fallback
is required. Persist source/build instructions in the workspace: scoring uses a clean copy of the
archived patch. Rebuild extensions on import or provide instructions for the fast model to build them;
do not rely on an unarchived binary or state from a previous session.

The reference Residual defines the math using black-box sublayers, exactly once each and in order,
independently per token. Its init_ may rescale sublayer weights by constants. For cross-sublayer
fusions implement class GPT(model.GPT) in a separate fast model and set model in submission/run.json.
A fast model must compute the same function with the same parameter names/shapes: rounding differences
only, no changed math, approximations or FP8. Keep a plain PyTorch reference for checking custom kernels.
The evaluator invokes unmodified check_equivalence.py with its default draws, tolerances and training
data before training a fast model. Run it yourself while developing; a failed check blocks scoring.

Set the entry paths and allowed knobs in submission/run.json: residual, optional model, steps, lr
(Muon), microbatch, checkpoint (boolean), compile (auto/model/sublayer/none), and val_every.
Configure AdamW lr/weight decay for residual parameters via optim_groups(). Omitted knobs retain
upstream defaults. Steps/lr are fixed before each run; training must never read validation data or
adapt to val loss. You may tune across runs, but may not cherry-pick seeds. Track, data, GPU allocation,
seed list and scoring protocol belong to the campaign, not the candidate.

{protocol}
Scored seeds: {list(seeds)}; GPUs: {gpus}. Compile warmup and full upstream validation are off-clock.
The configuration is at $EVOLVE_TASK/evaluation.json, including data_dir. For direct upstream commands,
link that dataset to data/fineweb10B first; the evaluator creates this link automatically when needed.
For a full development check use: python "$EVOLVE_TASK/evaluate.py" --workspace "$PWD" --output /tmp/UNIQUE_DIR
(Add --dry-run and omit --output to inspect commands without execution.) You can also invoke
check_equivalence.py and torchrun train_gpt.py directly with upstream flags. Keep useful test evidence
and explain your changes, measured results, and limitations. Do not claim unmeasured speedups."""
    rank = "val_loss" if score == "diagnostic-loss" else "train_seconds"
    spec = {
        "instruction": instruction,
        "evaluate": ["{python}", "{task}/evaluate.py", "--task", "{task}", "--output", "../raw/depthdash"],
        "evaluate_timeout_seconds": timeout + 120,
        "declaration": {"metrics": {"val_loss": {"direction": "min", "scale": "absolute", "format": "{:.5f}"},
            "train_seconds": {"direction": "min"}, "steps": {"direction": "max"},
            "peak_memory_gib": {"direction": "min"}, "evaluation_wall_seconds": {"direction": "min"},
            "target_pvalue": {"direction": "min"}},
            "rank_by": rank, "show": ["val_loss", "train_seconds", "steps"],
            "gate": ["frozen_sources", "equivalence", "completed", "target"], "artifact": "patch"},
        "prompt": {"feedback": "Read the archive. You may compile operators and run GPU tests/training during "
            "the session. A clean evaluation, including upstream fast-model equivalence, runs afterward."},
    }
    if session_seconds is not None:
        spec["time_limit_seconds"] = session_seconds
    (task / "task.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    return task


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--track", choices=("reference", "deep", "extreme_deep"), default="deep")
    parser.add_argument("--gpus", type=int, choices=(1, 2, 4, 8), default=8)
    parser.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    parser.add_argument("--score", choices=("diagnostic-loss", "time-to-target"), default="time-to-target")
    parser.add_argument("--budget", type=float, help="explicit timed budget for diagnostic-loss only")
    parser.add_argument("--steps", type=int, help="initial step count; candidate may tune it")
    parser.add_argument("--evaluation-timeout", type=int, required=True, help="total wall seconds for checking and all seeds")
    parser.add_argument("--session-seconds", type=int, help="agent wall-time limit; otherwise use framework default")
    args = parser.parse_args()
    print(prepare(args.upstream.resolve(), args.task.resolve(), args.data_dir, track=args.track, gpus=args.gpus,
                  seeds=args.seeds, score=args.score, budget=args.budget, steps=args.steps,
                  timeout=args.evaluation_timeout, session_seconds=args.session_seconds))
