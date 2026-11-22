# DepthDash adapter

Run InfiniEvolve's local adapter **inside an allocated GPU job**, using
[DepthDash at 96d683c](https://github.com/keyu-wang-2002/DepthDash/tree/96d683cf3284522e22696d9e2de135ff0859c2fd).
The submission scope follows the upstream [Rules](https://github.com/keyu-wang-2002/DepthDash/blob/96d683cf3284522e22696d9e2de135ff0859c2fd/README.md#rules).

## What the coding agent can do

- Add and edit residuals, helper modules, Triton/CUDA/C++ kernels, build scripts and tests.
  Only the four upstream files `train_gpt.py`, `model.py`, `tracks.py` and
  `check_equivalence.py` are frozen. The external campaign evaluator is not a submission file.
- Use Bash to compile, profile, run GPU tests, and perform repeated development training
  and equivalence checks within the allocated resources. The example has no file-only tool
  restriction, single-file edit restriction, mandatory CPU fallback, eight-minute session
  cap, $3 model cap, or three-session cap.
- Supply an optional `class GPT(model.GPT)` fast model for cross-sublayer fusions.
  The evaluator runs the original `check_equivalence.py` before training it, using upstream
  defaults and training data. It saves the output and requires a successful exit and PASS.
- Tune `steps`, Muon `lr`, `microbatch`, `checkpoint` and `compile` through
  `submission/run.json`; residual parameter groups can set AdamW lr/weight decay through
  `optim_groups()`. Omitted execution and optimizer settings retain upstream defaults.

The reference residual still calls black-box sublayers once each, in order, and works per
token; `init_` may rescale weights by constants. A fast implementation must preserve the
reference function and parameter names/shapes, with rounding differences only. No
approximate math or FP8. Training must not read validation data or adapt to validation
loss, and seeds cannot be cherry-picked. Source review remains necessary: checksums and
numerical equivalence do not prove every semantic rule or sandbox arbitrary candidate code.

For example, a step-based submission can contain:

```json
{
  "residual": "submission/residual.py",
  "model": "submission/fast_model.py",
  "steps": 2800,
  "lr": 0.03,
  "microbatch": 16,
  "checkpoint": false,
  "compile": "auto"
}
```

Omit `model` for a reference-only submission. Keep entry files and their helpers/kernels
in the same directory so upstream embeds them in its log. Commit source and reproducible
build logic, not an assumption that a binary from an earlier session survives: evaluation
runs in a fresh copy of the patch. `val_every` is also supported for upstream diagnostics.

## Scoring and resources

The official score is training time on **8 H100 SXM GPUs**, with multiple seeds supporting
mean final loss at or below the target at **p < 0.01**. Compile warmup and full upstream
validation are off-clock. `time-to-target` ranks mean training time over a fixed seed list
and gates it with a one-sided one-sample t-test on the reported losses. It requires SciPy in
the evaluation Python environment. Hardware must be allocated and verified by the caller; this adapter
alone does not certify a leaderboard submission.

**The pinned upstream has no calibrated target for `deep` or `extreme_deep`.** Preparation
therefore refuses time-to-target scoring for these tracks rather than inventing a target.
The `reference` track has defaults but is an upstream regression check, not a scored track.
When upstream publishes calibrated targets, update the pinned checkout and adapter pin.

For the currently uncalibrated tracks, explicitly select `diagnostic-loss` and a time budget.
This ranks mean final validation loss under the same timed budget for every candidate and
seed. Upstream `--budget` and `--steps` are mutually exclusive, so omit `steps` from
`run.json` for this mode. It is not an official record. Agents can still use upstream
step-based runs for development. GPU count (1/2/4/8), seeds and budget are campaign inputs,
not hardcoded pilot restrictions. Never compare scores from different protocols as if
they were one experiment.

Session limits, evaluator wall-time and campaign stopping rules are operational controls.
Set them to fit the GPU allocation, including compilation, agent development runs, warmup
and validation. Without `--session-seconds`, InfiniEvolve's standard one-hour session limit
applies; the campaign template uses its standard stopping defaults. `--evaluation-timeout`
is required and covers all seeds plus equivalence. It is separate from timed training.

## Prepare and preview

### Install ARIS and research before each session

The Claude agent loads [ARIS](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep)
as an explicit plugin and starts each session with `aris:research-lit`. Install the pinned
version on the same shared filesystem visible to the agent workers:

```sh
git clone https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep.git /path/to/aris
git -C /path/to/aris checkout 4ba94ae2a1c1fbbb14858750dd8764cabf6c28bd
export ARIS_REPO=/path/to/aris
claude plugin validate "$ARIS_REPO"
```

Export `ARIS_REPO` in the job launcher as well. The explicit `--plugin-dir` works even
with the template's empty `--setting-sources`; merely installing user skills would not
be enough to configure this isolated launcher. Skill, WebSearch and WebFetch are enabled.
The helper scripts resolve through `ARIS_REPO`. The web literature-review route does not
require Zotero, a Codex reviewer login, or a new paid search service; optional ARIS routes
may need their own setup. The full autonomous ARIS pipeline is not started by this template.

The [research-first instruction](RESEARCH_FIRST.md) is included in every prepared task.
Each session records sources, verification status, relevant approaches, rule compatibility,
and its next hypothesis in `research/REVIEW.md` before implementing. Notes and skill receipts
are archived with the candidate; later sessions refine them rather than repeating the whole
survey. This is an agent workflow instruction, not a new scoring gate or a claim that a file's
existence proves the quality of the research. Existing prepared tasks retain their original
prompt: prepare a new task/campaign from the updated code to adopt the instruction.

Read the effective session limit from the session request and reserve time to activate the
best tested candidate. Research time counts toward that limit; it does not change the
benchmark's model, kernel, or GPU-testing permissions.

Read the [complete agent prompt preview](PROMPT_PREVIEW.md) for the generated first-session
task text, appended system prompt and tool permissions. Its GPU count, seeds and 600-second
budget are explicitly labeled as example parameters; producing the preview runs no experiment.

Use a clean checkout at the pinned commit and a verified local FineWeb dataset. Record its
revision and shard checksums in the campaign. The evaluator does not download data. Use the
upstream CUDA/PyTorch environment (the pin requires Torch 2.11); compile dependencies for
chosen kernels must be available on the worker. Authenticate Claude through the existing
login/environment; keep credentials out of campaign files.

The following example only **prepares files**. Its 600-second budget, eight GPUs, three seeds
and three-hour evaluation timeout are editable resource choices, not submission rules:

```sh
python examples/depthdash/prepare.py \
  --upstream /path/to/DepthDash --task /path/to/deep \
  --data-dir /path/to/fineweb-shards --track deep \
  --gpus 8 --seeds 0 1 2 --score diagnostic-loss --budget 600 \
  --evaluation-timeout 10800

# Pure command preview: no GPU work, model import, data download or agent invocation.
python /path/to/deep/evaluate.py --task /path/to/deep \
  --workspace /path/to/deep/workspace --dry-run
```

Copy [`config.example.yaml`](config.example.yaml) into a **new** campaign directory, replace
`YOUR_MODEL` with the existing Claude Code model, and set `options.task_dir`. The folder name
must match `campaign.tasks`. Preview the generated prompt with:

```sh
python -m infinievolve.cli check /path/to/campaign --prompt
```

Preparation does not change existing generated tasks. Old pilot directories keep their old
configuration and evidence. Prepare a new task/campaign to use these rules; do not silently
mix it into the paused pilot or relabel old scores. Starting a campaign is a separate action:
`python -m infinievolve.cli run /path/to/campaign`, on the allocated worker, not the login node.

## Evidence and local tests

Every evaluation checks the four frozen files before execution, between GPU commands and
on exit, including failures. Fast-model equivalence runs before training; a failed or missing
PASS blocks scoring. Each seed is run with an explicit seed and one trial; missing, duplicate,
nonfinite or incomplete results fail. Full logs, invocation/configuration, per-seed metrics,
upstream source-containing logs and the final score are saved in the session's
`raw/depthdash/`, alongside InfiniEvolve's archived source patch. A baseline infrastructure
failure stops the campaign; a completed baseline that misses the loss target can still evolve.

Run `python -m pytest -q tests/test_depthdash.py` for local adapter tests. They verify command
planning and evaluator behavior with synthetic log fixtures at the GPU subprocess boundary;
they make no model calls and run no training, kernel compilation or GPU experiments. Actual
kernel correctness and performance remain unmeasured until a separately authorized GPU run.
