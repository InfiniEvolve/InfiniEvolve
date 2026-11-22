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
