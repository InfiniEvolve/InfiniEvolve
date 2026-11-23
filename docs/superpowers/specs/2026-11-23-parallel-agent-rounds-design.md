# Parallel agent rounds

Status: proposed; implementation and upstream PR pending design confirmation.

## Intent and constraints

Allow a single InfiniEvolve task to launch several independent agent sessions per round. After all members have finished evaluation and required confirmation, select the next round's starting material using greedy selection, probability sampling, or a genetic algorithm. Default campaigns must retain today's sequential behavior and existing adapter contracts.

This change belongs entirely in the upstream InfiniEvolve core. InfiniEvolve-Research remains two submodules and a skill mounting script; its pinned core is not changed to an unmerged branch. There is no new research workflow, metric, evaluator, or agent runtime. Do not merge the resulting PR.

## Approaches

1. Merely remove the scheduler's busy-task check. This permits concurrency but gives later submissions a different archive depending on which sibling finishes first, with no defined round or population. Rejected: it does not implement round-based selection.
2. Persist explicit rounds, freeze their input history, and wait at a round barrier before selection. Recommended: it fits the existing filesystem-backed scheduler and makes parentage, restart, and selection auditable.
3. Run a second coordinator outside the core. Rejected: the user explicitly requires one core scheduler and an upstream PR.

The implementation approach is a proposal, not a decision already approved by the user. Agent-mediated genetic crossover is also pending confirmation.

## Configuration and compatibility

Proposed public configuration:

```yaml
campaign:
  # Existing adapter/tasks/name omitted here.
  agents_per_round: 3     # default 1
  max_parallel: 3         # existing global job limit, including evaluations
strategy:
  selection: genetic     # greedy | probabilistic | genetic
  seed: 0                # existing seed
  temperature: 1.0       # probabilistic selection and genetic fitness sampling
  population_size: 6     # genetic only; default agents_per_round, minimum 1
  elite_count: 1         # genetic only; default 1, <= population_size
  crossover_rate: 0.8    # genetic only, [0, 1]
  mutation_rate: 0.2     # genetic only, [0, 1]
stop:
  max_sessions: 8         # total agent sessions, NOT rounds: 3 + 3 + 2
  window_k: 6             # still measured in sessions
```

- `agents_per_round` is a positive integer. `max_parallel` remains the actual concurrency ceiling: a larger round can run in waves without changing its parent/history snapshot.
- If `strategy.selection` is absent and width is 1, use the existing sequential path with `strategy.start: best|explore|baseline`, including its current RNG, archive, stop, and prepared-session behavior. This is the default compatibility guarantee.
- If width exceeds 1 and selection is omitted, use greedy selection. Reject a non-default legacy `strategy.start` combined with round mode, with a message directing the user to `strategy.selection`. Do not silently ignore a legacy strategy.
- Explicit `strategy.selection` enables round semantics even at width 1.
- Allow the new settings as task overrides, using the same flattened convention as existing `start`/`max_sessions` overrides. Validate effective task settings, not just global defaults. Reject non-finite temperatures, invalid probabilities, Boolean/float widths, and impossible population/elite settings.
- Configuration changes take effect at the next round. An open round keeps its recorded width, policy, RNG inputs, and parent choices. Legacy prepared sessions are drained before entering round mode. Disabling round mode cannot abandon an open round.
- On changing the selection policy or genetic population size, initialize the new policy from all currently eligible archived nodes in deterministic score/index order: a best incumbent for greedy/probabilistic, or the best distinct `population_size` individuals for genetic. Record the old/new settings and initialization pool as an explicit transition. Re-entering round mode after intervening legacy sessions uses this same initialization rather than stale round state. Changes only to width, temperature, seed, elite count, or variation rates retain the previous incumbent/population and apply the new values to the next plan. This transition rule is the author's recommendation following builder review.

## Round lifecycle

Each task stores `rounds/rNNN.json` with an immutable input snapshot, ordered member session IDs, selection settings, per-member parent IDs and variation decisions, and a final outcome. Round records are atomically written before any member is submitted.

1. Finish baseline evaluation and any required baseline confirmation once.
2. At a boundary, apply the existing stop rule and hold control. Reserve no more than the remaining session budget. Allocate session IDs above all existing/reserved IDs, including void and prepared sessions.
3. Snapshot the visible archived nodes and fixed parent artifacts. Prepare each member from that snapshot. A sibling must never enter another member's prompt or parent selection, even if the global concurrency ceiling forces waves.
4. Fill available global slots fairly across tasks and round members. A task may have several session jobs, but never duplicate baseline submissions or the same session twice. Confirmation jobs also consume global slots.
5. Archive results with the existing evaluator, validity rules, flags and duplicate-score pooling. Wait for all members and confirmations before final selection. Failure of one member must not prevent already-running siblings from being collected.
6. Persist the selection outcome, then open the next round if its stop/hold checks allow it.

`max_sessions` and `window_k` retain their existing units. Plateau checks happen only at round boundaries; already reserved members are allowed to finish. Invalid and hard-flagged sessions consume budget but cannot become parents. Void sessions do not consume the session budget; the completed round can therefore be followed by replacement work subject to the remaining budget. A final round may be shorter. The user-facing state distinguishes round number, reserved members, completed members, selected parents, and global in-flight jobs.

For confirmation, evaluate each eligible round candidate against the pre-round best after all members have archived, rather than confirming whichever candidate happened to finish first. Record adapter-requested remeasurement immediately but defer selection until all required samples complete. Process candidates in session-index order. Pool identical artifacts consistently before final selection. This makes required confirmation independent of completion order.

## Selection policies

All policies use the task's existing declared ranking metric and validity rules. Sort candidates by session index for deterministic ties and draws. Record candidate IDs, effective scores, probabilities, seed, draws, and outcome. Scores remain withheld from agent prompts when `hide_scores` is enabled.

### Greedy

Choose the best eligible candidate from the previous incumbent plus this round's offspring, with earliest-session tie breaking. Keep the incumbent if every offspring is invalid or worse. The first incumbent is the best eligible archived node when round mode begins; if none exists, use the baseline as an unranked fallback. All members of the next round inherit the selected incumbent.

### Probabilistic

Use the same incumbent-plus-offspring candidate pool. Assign descending fitness ranks (equal metric values receive equal rank) and use a numerically stable softmax of rank utility divided by `temperature`. Rank utility is normalized to [0, 1], so metric units and min/max direction do not distort the temperature. Higher temperature approaches uniform sampling; lower temperature favors better candidates. Draw one incumbent for the next round with a deterministic RNG keyed by campaign seed, task, round, and selection purpose. Keep the selected incumbent even if it is worse than the all-time best; the best archive entry remains separately visible/exportable.

No eligible candidate means baseline fallback. A one-candidate pool has probability 1. No NaN, zero-sum, overflow, or invalid candidate may enter sampling.

### Genetic

Maintain up to `population_size` distinct eligible individuals. Initialize from the best eligible archived candidates (usually only the baseline). Deduplicate by artifact fingerprint where available, otherwise by session ID.

At each round boundary, form the union of the previous population and valid offspring. Preserve the best `elite_count` individuals, then sample the remaining survivors without replacement using the same fitness probabilities. Keep fewer individuals if too few distinct eligible artifacts exist; never fabricate population members. The archived all-time best is separately reported.

For each next-round member, sample a primary parent from this persisted population. With `crossover_rate`, sample a distinct secondary parent, if one exists. Independently sample whether to request mutation with `mutation_rate`. Persist these draws before launch. A single available parent yields mutation/reproduction without a second parent. With neither crossover nor mutation, request faithful reproduction and still evaluate the result; the core does not claim to enforce an LLM's compliance.

The workspace starts from the primary parent using the existing `start_artifact` contract. For crossover, include the complete rendered secondary artifact and explicit instructions to combine useful parts with the primary, then apply the sampled mutation instruction. Store both parent IDs and the variation decisions in the session spec and node. Agent-mediated variation is proposed because artifacts may be code patches or complete documents; arbitrary byte splicing would be invalid.

Initially support crossover only for artifact kinds that can render their full content (the built-in patch and text kinds). A nonempty donor whose renderer returns no content fails explicitly before submission; do not silently pretend crossover occurred. A root/empty artifact is described as baseline/empty work. Explicit genetic mode makes its selected donor visible even with `carry: none|reports`; document that these options still control historical archive visibility, whereas the selected donor is genetic input. Scores stay hidden as configured.

Agent-mediated crossover/mutation is an instruction and recorded input, not proof that the model actually performed the intended edit. Tests and documentation must preserve that distinction.

## Restart and failure handling

- Reattach existing jobs using the current adapter contract. Never change inputs of an already-submitted member.
- Regenerate a never-submitted member's prompt using current task instructions, as upstream PR #7 requires, but keep its round snapshot, parent choice, and variation draws. A sibling finishing or a restart cannot change those inputs.
- A host fault or unavailable model retries the same member slot and parentage; preserve the existing global availability hold. A hard job failure pauses new work for that task while other submitted siblings are collected.
- A crash between result archival and round finalization resumes finalization idempotently. A crash during preparation must not allocate a duplicate ID or begin an extra round. Do not claim exactly-once external submission across the existing submit/job-persistence crash gap.
- Hold prevents additional member submissions, including reserved but unsubmitted members; running jobs finish. An open held round remains resumable and does not look like a completed campaign.
- Operator flag/clear changes are honored when selecting at the boundary. Excluded selected parents invalidate a pending unsubmitted plan explicitly rather than silently using them; require a new plan from the eligible pre-round snapshot, recorded as a revision. Submitted siblings keep their original lineage.

## Scope of code changes

Configuration parsing and template; persistent round/planning module; scheduler filling and task loop; selection helpers; optional round/parent/variation metadata on SessionSpec and Node; compact archives and state/exports; prompt donor context; README and adapter compatibility documentation. Keep `parent_id` as primary-parent compatibility field. New dataclass fields have defaults and old campaigns remain readable. Existing third-party adapters continue to receive the original primary artifact and prompt; no new mandatory adapter methods.

## Validation and acceptance

1. Run the full existing core suite unchanged to verify default sequential behavior and prior restart/confirmation fixes.
2. Add controlled-adapter tests that keep jobs pending: prove N overlapping sessions for one task, the global ceiling, fairness across tasks, one baseline only, identical same-round history, and no next round before all required work finishes.
3. Cover partial final rounds, hold/resume, invalid/hard/void outcomes, all-invalid fallback, retries, model unavailability, interrupted preparation and finalization, switching configurations, and confirmation/duplicate pooling under different completion orders.
4. Test all policies for min/max and equal scores, invalid exclusion, seed determinism across restart and completion order, positive finite probabilities, elitism, distinct parents, fingerprint deduplication, and variation-rate endpoints.
5. Test complete donor delivery and primary restoration through LocalAdapter, including `carry` and `hide_scores`. Test unsupported artifact failure explicitly.
6. Run a small real-agent two-round smoke task with two agents per round and genetic crossover/mutation forced on, inspecting both parent inputs, agent edits, evaluator verdicts, and recorded lineage. Keep budget limited; this demonstrates execution, not research-quality gains or genetic-search superiority. If authentication/quota blocks the model run, report the exact limitation and do not label a fake adapter as a real agent.
7. Submit an upstream PR with implementation, user documentation, exact validation evidence, and limitations. Leave it unmerged and leave the Research submodule pin unchanged.

Implementation details such as helper names, file organization, stable seed serialization, and individual test fixture layout are left to the builder, subject to these behaviors. Proposed defaults and policy semantics above are author recommendations awaiting confirmation.
