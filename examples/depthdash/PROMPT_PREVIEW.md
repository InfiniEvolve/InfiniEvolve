# DepthDash agent prompt preview

Generated from this branch's [`prepare.py`](prepare.py), [`RESEARCH_FIRST.md`](RESEARCH_FIRST.md)
and [`config.example.yaml`](config.example.yaml). ARIS is pinned to
`4ba94ae2a1c1fbbb14858750dd8764cabf6c28bd`.

**The GPU count (8), seeds (0, 1, 2), and 600-second timed training budget per seed
are example parameters for this preview. They do not specify a newly launched experiment
or impose these values on every campaign.** The 600 seconds cover timed training, not
compilation, validation, research or the entire agent session. Select actual campaign
resources separately; see [scoring and resources](README.md#scoring-and-resources).

Generated with `python -m infinievolve.cli check /path/to/campaign --prompt`.
No agent, compilation, training or GPU job was started to produce this preview.

This is the complete adapter-supplied first-session task prompt and appended system prompt.
Claude Code supplies its own built-in system prompt and loads the ARIS skill body when the
agent invokes `aris:research-lit`. Later sessions receive their actual archive and starting
point. The effective time limit is in the runtime session request, not these example values.

## Appended system prompt

```text
At the start of every session, follow the task's research-first instructions and invoke aris:research-lit before implementing. Read the full archive at $EVOLVE_ARCHIVE, update research/REVIEW.md with verified sources and the next hypothesis, and follow the task's DepthDash rules. You may write Triton/CUDA operators, compile them and run GPU tests or development training. Read the effective session time limit before scheduling long tests, and select your best supported submission before that deadline.
```

## Plugin and tool permissions

```text
--plugin-dir "$ARIS_REPO"
--permission-mode acceptEdits
--tools 'Read,Glob,Grep,Edit,Write,Bash,Skill,WebSearch,WebFetch'
--allowedTools 'Read' 'Glob' 'Grep' 'Edit' 'Write' 'Bash' 'Skill' 'WebSearch' 'WebFetch'
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
# Research before implementation — every session

Begin every session with a focused literature and implementation review, before changing
submission code or starting compilation/training. Read the task rules, the full archive at
`EVOLVE_ARCHIVE`, and any existing `research/` notes first. Invoke the installed ARIS skill
`aris:research-lit` with `— sources: web` for the next unresolved DepthDash question. Use
the skill's paper-identity verification helper; `ARIS_REPO` points to the pinned plugin
and its `tools/` directory. WebSearch and WebFetch are available alongside Bash. If a
search service fails, try another public primary source or the ARIS arXiv helper. Record
access failures and unverified claims honestly; do not invent citations or call an empty
search a completed review.

Study both residual/normalization designs for deep transformers and relevant efficient
implementations. Read DepthDash's existing baselines and fast models so a known technique
is not presented as a new invention. Follow useful papers to their original text and
official code. Distinguish a reference-math change (allowed residual design), a mathematically
equivalent fast implementation, and execution/hyperparameter tuning. All remain available;
Triton/CUDA/C++ operators, compilation, profiling, and GPU tests are explicitly permitted
under the original DepthDash rules. Do not force an architecture change without evidence.

Before implementing, write or update `research/REVIEW.md` in the session workspace with:

- This session's question, queries, sources actually read, URLs/IDs, verification status,
  and code revision where available. Preserve the skill's evidence and verification receipts.
- Relevant mechanisms and the evidence for them; clearly separate published measurements,
  local measurements, hypotheses, and unknowns.
- A comparison of promising approaches, their fit with DepthDash's rules, expected quality
  and speed tradeoffs, and the next candidate's testable hypothesis.
- The selected change, what it changes (math, implementation, or settings), the smallest
  useful validation, and why it is preferable to merely repeating previous tuning.

Later sessions should reuse verified notes and update only gaps, new evidence, and the next
hypothesis. Do not repeat a broad survey from scratch. Keep research in proportion to the
available session time; this is a preparation step, not an instruction to launch ARIS's
entire autonomous research/paper pipeline or external notifications.

Read the effective `time_limit_seconds` from the sibling `request.json` next to
`EVOLVE_PROMPT` before planning long calls, and honor any earlier wrapper deadline in the
system prompt. Reserve time to finish or stop development processes, put the best supported
candidate in `submission/run.json`, and write the final session report. Do not leave a better
tested candidate unselected while starting another long optional benchmark. These are
operational time limits, not restrictions on the benchmark's permitted optimization scope.

Keep research artifacts in the workspace so InfiniEvolve archives them with the candidate
and can carry them into later sessions. In the final session report, identify the research
that informed the change and link `research/REVIEW.md` and any supporting test logs.

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
