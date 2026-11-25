# Parallel Agent Rounds Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans in this session. Track completion below.

**Goal:** Configurable per-task parallel rounds with greedy, probabilistic, and genetic selection, preserving default sequential behavior.

**Architecture:** Keep the legacy path intact. A persisted round controller manages immutable history, member plans, confirmation barriers and deterministic selection. Extend existing session metadata and prompts without requiring new adapter methods.

**Tech Stack:** Python 3.10+, existing PyYAML and pytest; no new runtime dependencies.

**Spec:** ../specs/2026-11-23-parallel-agent-rounds-design.md

## Global Constraints

- Default agents_per_round = 1; max_sessions counts sessions.
- Existing adapters and old campaigns remain readable.
- Agent performs crossover/mutation; the task evaluator remains unchanged.
- Publish an upstream PR, do not merge or change Research's submodule pin.

## Review Focus

- Resume with configuration changes must finish the pinned open round.
- Partial preparation cannot reuse a reserved session ID.
- Completion order and duplicate pooling cannot change required confirmation.
- Tiny temperature, equal fitness and min metrics must yield valid probabilities.
- Pauses, holds and unavailable models must not lose running siblings.

### Task 1: Configuration and pure policy functions

Files: infinievolve/config.py, infinievolve/core/round_selection.py, tests/test_round_selection.py.
Interfaces: policy(settings) -> dict; choose(policy, nodes, metric, seed) -> audit dict; parents(policy, population, metric, seed) -> audit dict.
- [x] Add tests for defaults, overrides, invalid configuration, softmax ties/direction/extremes, deterministic sampling, elites, deduplication and rate endpoints; run and observe failure.
- [x] Implement strict new configuration validation and pure deterministic selection/parent plans.
- [x] Run `.venv/bin/python -m pytest -q tests/test_round_selection.py` (using the Research venv absolute path), expect all pass; commit.

### Task 2: Persistent rounds and scheduler integration

Files: core/rounds.py, core/loop.py, core/types.py, core/archive.py, core/views.py, tests/test_rounds.py.
Interfaces: RoundController(loop).active() -> dict|None; next_job() -> Job|None; round status from files.
- [x] Add controlled-adapter tests for overlap, bounded waves, fair scheduling, identical history, barrier, partial final rounds, restarts, held work and failure recovery; observe failures.
- [x] Implement round persistence, pinned settings and parent plans, donor prompt delivery, deterministic confirmations, metadata and state, and fair global slot filling.
- [x] Add transition, flag, void, duplicate, hidden score and unsupported donor tests.
- [x] Run all tests, expect existing and added tests pass; commit.

### Task 3: Documentation and end-to-end verification

Files: README.md, infinievolve/templates/campaign.yaml, docs/parallel-rounds.md, tests/test_round_local.py.
- [x] Test LocalAdapter primary restoration and donor delivery through actual child processes; clearly label scripted models.
- [x] Document configuration, round lifecycle, selection formulas, genetic semantics, carry override, resume behavior and adapter limits.
- [x] Run a bounded real Codex task with 2 agents x 2 rounds and forced crossover/mutation. Verify process overlap, parent inputs, actual edits and evaluator output, then summarize in a results table.
- [x] Complete independent whole-branch review and address consequential findings with regression tests. Final core suite: 95 passed, 1 skipped; Research suite against changed core: 6 passed.
- [x] Commit and push the feature branch; created https://github.com/InfiniEvolve/InfiniEvolve/pull/8 against main. Leave the PR unmerged and the Research core pin unchanged.

## Progress and decisions

- Baseline: 58 passed, 1 skipped before implementation.
- User confirmed Agent-mediated crossover/mutation. Proceed inline under the user's existing request to implement and submit the core PR; no additional execution-method approval is needed.

- Task 1 complete: 15 policy/config tests passed after initial missing-module failure.
- Task 2 complete: concurrency/barrier tests failed against the sequential loop, then passed; full core suite now 89 passed, 1 skipped.
- Task 3 in progress: real 2x2 Codex genetic run completed; all four archived patches independently re-evaluated successfully, both crossover offspring passed 6/6 cases.

- Final review (ca479ab): five consequential findings accepted; the additional unverified ID-gap scenario was reproduced and elevated because it could overwrite archived inputs.
- Final fixed: width-only changes retain probabilistic incumbent — test_width_change_preserves_probabilistic_incumbent RED→GREEN.
- Final fixed: retained genetic copies are filtered before deduplication — test_retained_genetic_duplicate_is_not_lost_to_old_archive_id RED→GREEN.
- Final fixed: opening a new round invalidates old stop status — test_opening_new_round_clears_old_stop_status RED→GREEN.
- Final fixed: operator exclusions overlay frozen history and replan from eligible pre-round candidates — test_newly_excluded_parent_is_hidden_and_replanned_from_snapshot RED→GREEN.
- Final fixed: finalization re-pools after interrupted confirmation — test_crash_after_last_confirmation_outcome_reconciles_pool RED→GREEN.
- Final fixed: legacy allocation follows maximum archived index after round gaps; preparation refuses submitted/archived IDs — test_switch_to_legacy_after_partial_directory_keeps_ids_monotonic RED→GREEN.
- Final minor (deferred): check --prompt previews the base session prompt, omitting genetic variation instructions. Actual submitted and archived prompts include them; document the preview limit.
- Final review declined to judge: no real-model search-quality claim is made; scripted tests establish scheduler behavior and the real smoke establishes bounded model execution only.

- Final verification: core 95 passed/1 skipped; Research integration 6 passed. Final-code real 2x2 rerun passed all four archived-patch evaluations and both donor/primary checks.
