# Parallel agents for one task

By default, one task runs one agent session at a time. To run several agents in
one round, edit the run's `config.yaml` (for a task run, `runs/<name>/config.yaml`):

```yaml
campaign:
  adapter: local
  tasks: [your-task]
  agents_per_round: 3
  max_parallel: 3
  poll_seconds: 1
strategy:
  selection: greedy       # greedy | probabilistic | genetic
  seed: 0
stop:
  max_sessions: 8         # total sessions: rounds of 3, 3, then 2
  window_k: 8
options:
  task_dir: ../..         # relative to this config, for a single-task run
```

`max_parallel` limits all jobs across all tasks, including baseline evaluation
and repeat scoring. `agents_per_round` controls the number of candidate sessions
for each task. If the global limit is smaller, a round runs in waves. Tasks take
turns receiving available slots. Each session still gets its own workspace.

All members of a round receive the same historical archive snapshot. Their
parents and random choices are saved before any member starts. Finished siblings
are not added to another member's input. The next round waits for every member
and all required confirmation scoring. A failed job pauses further submissions
for that task; already-running siblings are still collected.

## Selection

All policies use the task's existing metric and validity checks. Invalid,
hard-flagged and void results cannot be parents. The all-time best remains visible
and exportable even when a sampling policy selects a worse incumbent.

| Policy | Candidates | What starts the next round |
|---|---|---|
| `greedy` | Previous incumbent plus this round's offspring | Best valid result; ties favor the earlier session |
| `probabilistic` | Previous incumbent plus this round's offspring | One fitness-weighted sample |
| `genetic` | Previous population plus this round's offspring | A population with elite retention and sampled survivors; each agent draws its own parents |

For sampling, sort the distinct scores from worst to best, respecting `min` or
`max`. Map their ranks evenly onto `[0, 1]` (one distinct score maps to 0).
Candidates with the same score have the same rank. Probability is proportional
to `exp((rank - best_rank) / temperature)`. The subtraction avoids overflow.
`temperature` must be finite and positive; smaller values favor the better
candidates, larger values approach uniform sampling. Under extreme temperatures,
very small probabilities can round to zero. Seeds include task, round, and draw
purpose. Selection is reproducible from the persisted inputs, independently of
job completion order; model outputs themselves are not deterministic.

If no candidate is eligible, the next round starts from the baseline. A task
with an invalid baseline can therefore still search for its first valid result.

A survivor that is hard-flagged or voided between two rounds gives up its place:
the next round fills it with the best remaining eligible candidate that does not
hold the same work (by fingerprint), and records the swap under `refill` in the
round file. A parent excluded after its round opened is replanned from that
round's snapshot instead. Replacement parents also participate in survivor
selection alongside the original population and offspring, including after a restart.

## Genetic selection and variation

```yaml
strategy:
  selection: genetic
  seed: 0
  temperature: 1.0
  population_size: 6       # default: agents_per_round
  elite_count: 1           # 0..population_size
  crossover_rate: 0.8      # 0..1, chance of requesting a second parent
  mutation_rate: 0.2       # 0..1, independent chance of requesting mutation
```

The population contains distinct eligible artifacts, deduplicated by fingerprint
when the artifact kind supports one. Initialize with the best existing archived
individuals. At each boundary, preserve the best `elite_count` from the union of
parents and offspring, then sample remaining survivors without replacement. If
there are too few distinct valid results, keep a smaller population.

For each offspring, sample a primary parent, then optionally a distinct secondary
parent. The adapter restores the primary artifact exactly as before. The prompt
includes the complete rendered secondary artifact and asks the agent to combine
useful parts. A separate draw asks for mutation. With only one parent available,
there is no crossover. With neither crossover nor mutation, the agent is asked
to reproduce the primary unchanged and its result is still evaluated.

**The agent performs the edits.** The core records which operations it requested;
it cannot guarantee that a model obeyed them. Genetic metadata is evidence of
inputs and selection, not proof that a meaningful crossover or mutation occurred.
The same original evaluator scores the resulting work.

Built-in `patch` and `text` artifacts provide donor rendering. A custom artifact
kind must render its complete content; a nonempty donor returning no rendering
causes an explicit error before submission. Selected genetic donor material is
provided even with `prompt.carry: none` or `reports`: those settings still govern
historical archive visibility. `hide_scores` still hides framework-rendered
scores; artifact contents are supplied unchanged.

## Budgets, confirmation, hold and restart

- `max_sessions` counts agent sessions, not rounds. Invalid/hard-flagged sessions
  consume budget; void sessions do not. A final round can be shorter.
- `window_k` still counts sessions. Plateau checks occur at round boundaries;
  reserved members finish even if a partial round would have triggered a stop.
- Confirmation compares each eligible offspring with the best pre-round node
  using its current pooled score and eligibility, in session-index order. The
  historical inputs sent to agents remain frozen. All required repeats finish
  before survivor selection. This can require more repeats than sequential runs.
- `task_overrides.<task>.hold: true` prevents additional round member submissions,
  including prepared members. Submitted jobs finish; the open round remains saved.
- Resume with the usual `infinievolve run <run-directory>`. Submitted jobs reattach.
  A never-submitted member gets updated task instructions but keeps its round's
  history, parents, and random choices. Host faults and model unavailability retry
  the same member; the existing campaign-wide availability hold still applies.
- Round settings are pinned for an open round. New settings apply at the next
  boundary. Old prepared sequential sessions finish before entering round mode.
  Disabling rounds first drains an open round.
- Changing selection policy or effective genetic population size initializes from the
  currently eligible full archive, recording that transition. Re-entering rounds
  after sequential sessions also reinitializes. Other policy changes retain the
  previous incumbent/population. Excluding a planned parent revises unsubmitted
  members from eligible pre-round snapshot members; submitted lineage is retained.

The existing gap between external job submission and saving its job handle is
unchanged: this does not promise exactly-once external execution across a crash
in that gap. Run one scheduler process per campaign, as before.

## Compatibility and inspection

Preview limitation: `infinievolve check --prompt` shows the base session prompt;
it does not include the genetic variation section. Inspect the submitted
`input/prompt.md` for the complete prompt actually delivered to an agent.

Without `strategy.selection` and with `agents_per_round: 1`, the original
sequential path, including `strategy.start: best|explore|baseline`, is retained.
Multiple agents with no selection specified use greedy selection. Round mode
requires the default legacy `start: best`; use the new `selection` field to
choose the round policy. Explicit selection enables rounds even at width 1.

All new fields can be overridden per task using the existing flattened format:

```yaml
task_overrides:
  expensive-task:
    agents_per_round: 2
    selection: probabilistic
    temperature: 0.5
```

Third-party adapters need no new methods. `SessionSpec.start_artifact` is still
the primary artifact; donor material is in `SessionSpec.prompt`. New optional
fields `round_id`, `parent_ids` and `variation` preserve lineage on specs and
nodes. `parent_id` remains the primary parent for existing consumers.

| Evidence | File |
|---|---|
| Frozen history, members, parent draws, policy and outcome | `tasks/<task>/rounds/rNNN.json` |
| Input actually sent to a member | `tasks/<task>/sessions/sNNN/input/prompt.md` and `spec.json` |
| Active/latest round and completed members | `state.json`, under the task's `round` |
| Both genetic parents and variation request | Node JSON, compact archive, `exports/nodes.csv` |
| Both ancestry edges | `exports/<task>/lineage.{json,dot,mmd}` |

Round JSON and exports contain scores even when agent prompts hide them. They
are operator records, not additional agent inputs.
