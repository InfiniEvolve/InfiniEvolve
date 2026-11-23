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
- [ ] Add tests for defaults, overrides, invalid configuration, softmax ties/direction/extremes, deterministic sampling, elites, deduplication and rate endpoints; run and observe failure.
- [ ] Implement strict new configuration validation and pure deterministic selection/parent plans.
- [ ] Run `.venv/bin/python -m pytest -q tests/test_round_selection.py` (using the Research venv absolute path), expect all pass; commit.

### Task 2: Persistent rounds and scheduler integration

Files: core/rounds.py, core/loop.py, core/types.py, core/archive.py, core/views.py, tests/test_rounds.py.
Interfaces: RoundController(loop).active() -> dict|None; next_job() -> Job|None; round status from files.
- [ ] Add controlled-adapter tests for overlap, bounded waves, fair scheduling, identical history, barrier, partial final rounds, restarts, held work and failure recovery; observe failures.
- [ ] Implement round persistence, pinned settings and parent plans, donor prompt delivery, deterministic confirmations, metadata and state, and fair global slot filling.
- [ ] Add transition, flag, void, duplicate, hidden score and unsupported donor tests.
- [ ] Run all tests, expect existing and added tests pass; commit.

### Task 3: Documentation and end-to-end verification

Files: README.md, infinievolve/templates/campaign.yaml, docs/parallel-rounds.md, tests/test_round_local.py.
- [ ] Test LocalAdapter primary restoration and donor delivery through actual child processes; clearly label scripted models.
- [ ] Document configuration, round lifecycle, selection formulas, genetic semantics, carry override, resume behavior and adapter limits.
- [ ] Run a bounded real Codex task with 2 agents x 2 rounds and forced crossover/mutation. Verify process overlap, parent inputs, actual edits and evaluator output, then summarize in a results table.
- [ ] Run core and Research integration suites against the changed core; independent whole-branch review; address consequential findings with regression tests.
- [ ] Commit, push the feature branch, create upstream PR and verify its URL/state. Leave Research core pin unchanged.

## Progress and decisions

- Baseline: 58 passed, 1 skipped before implementation.
- User confirmed Agent-mediated crossover/mutation. Proceed inline under the user's existing request to implement and submit the core PR; no additional execution-method approval is needed.
