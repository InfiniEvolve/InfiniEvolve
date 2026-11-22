# DepthDash agent prompt preview

Snapshot of the prompt generated from commit `74cd7e98ebd60cd2477faf4e79ea130865b82d50`,
using [`prepare.py`](prepare.py) and [`config.example.yaml`](config.example.yaml).

**The GPU count (8), seeds (0, 1, 2), and 600-second timed training budget per seed
are example parameters for this preview. They do not specify a newly launched experiment
or impose these values on every campaign.** This snapshot uses the README's `deep` /
`diagnostic-loss` example; select actual resources, seeds and scoring settings when preparing
a future campaign. The 600 seconds cover timed training, not compilation, validation or
the entire agent session. See [scoring and resources](README.md#scoring-and-resources)
for the separate official benchmark requirements.

Generated with `python -m infinievolve.cli check /path/to/campaign --prompt`.
No agent, compilation, training or GPU job was started to produce this preview.

This contains the complete adapter-supplied first-session task prompt and appended system
prompt template, including the explicit permission to write Triton/CUDA operators, compile
them and run GPU tests. Claude Code manages its own built-in system prompt. At runtime,
`EVOLVE_ARCHIVE` expands to that session's archive path; later sessions also receive their
actual starting point and archive context. The task prompt below is preserved verbatim.

## Appended system prompt

```text
Read the full archive at $EVOLVE_ARCHIVE and follow the task's DepthDash rules. You may write Triton/CUDA operators, compile them and run GPU tests or development training.
```

## Tool permissions

```text
--permission-mode acceptEdits
--tools 'Read,Glob,Grep,Edit,Write,Bash'
--allowedTools 'Read' 'Glob' 'Grep' 'Edit' 'Write' 'Bash'
```

## Complete first-session task prompt

```text
You are working on the task below. When you end this session, what you leave behind is evaluated and archived.

## How this session works

- Work in the directory you are started in. Whatever differs from its starting state when you end the session is your work.
- How your work is ranked: by `val_loss`, lower being better, among versions that pass `frozen_sources`, `equivalence`, `completed`, `target`.
- Read the archive. You may compile operators and run GPU tests/training during the session. A clean evaluation, including upstream fast-model equivalence, runs afterward.
- You decide when the session is over. Keep going while you still see an untried idea with a realistic chance of a meaningful gain, and end the session once further work is unlikely to pay off. Before ending, make sure what you leave behind is your best valid work. Your last message must contain this block:

SESSION-REPORT-BEGIN
approach: <one paragraph: what your final work does>
results: <what you measured or observed, and which version was best>
worked: <bullets>
did_not_work: <bullets, each with the reason>
next: <the most promising directions for the next session>
SESSION-REPORT-END

## Task

Optimize DepthDash's deep track (L28, d512), starting from Pre-LN.
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

This campaign is a diagnostic: rank mean final val_loss after 600.0 timed seconds per seed. DepthDash --budget and --steps are exclusive. This is not a leaderboard record.
Scored seeds: [0, 1, 2]; GPUs: 8. Compile warmup and full upstream validation are off-clock.
The configuration is at $EVOLVE_TASK/evaluation.json, including data_dir. For direct upstream commands,
link that dataset to data/fineweb10B first; the evaluator creates this link automatically when needed.
For a full development check use: python "$EVOLVE_TASK/evaluate.py" --workspace "$PWD" --output /tmp/UNIQUE_DIR
(Add --dry-run and omit --output to inspect commands without execution.) You can also invoke
check_equivalence.py and torchrun train_gpt.py directly with upstream flags. Keep useful test evidence
and explain your changes, measured results, and limitations. Do not claim unmeasured speedups.

## Starting point

You start from the unmodified baseline.
```
