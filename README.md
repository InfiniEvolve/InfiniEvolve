English | [简体中文](README.zh-CN.md)

# InfiniEvolve

An outer loop over coding-agent sessions. For each task it keeps a lineage: every session starts from
an earlier session's work, is told what the earlier ones tried, and is archived with everything it
produced. The loop decides where the next session starts and when the task has stopped improving.

The framework owns the loop and the archive. It does not know what a task is about: what is measured,
which way is better, what makes a result valid, what a session's work looks like and where an agent runs
all come from outside it.

## Quick start

Needs Python 3.10 or newer; the example also needs `git`.

```sh
git clone https://github.com/xiemeimeiaaa/InfiniEvolve.git && cd InfiniEvolve
uv venv -p 3.11 .venv && uv pip install -p .venv -e .

.venv/bin/infinievolve run examples/local/campaigns/demo
```

The example task asks for a correct program that is as short as possible, and a scripted stand-in plays
the agent, so it runs in a few seconds without any account. It prints each session as it is archived and
ends with the summary:

```
[golf/s000] archived final: bytes 466 (valid); wall 0.0 min; best so far 466
[golf/s001] submitted, start from s000 (best); limit 10 min
[golf/s001] archived final: bytes 286 (valid); agent 0.0 min; wall 0.0 min; best so far 286
...
[golf] stop: best 163 within 0.0 of 163 over the last 2 sessions

| task | ranked by | sessions | valid | baseline | best | best session | agent h | status |
|---|---|---|---|---|---|---|---|---|
| golf | bytes | 7 | 7 | 466 | 163 | s005 | 0.00 | best 163 within 0.0 of 163 over the last 2 sessions |
```

## What you get

Everything a campaign produces is in its own directory. The parts you will look at:

| You want | Look at |
|---|---|
| how each task ended up | `summary.md` (also printed by `infinievolve report`) |
| one task session by session: every metric, the best so far, flags | `tasks/<task>/archive/matrix.md` (`infinievolve report --task <task>`) |
| the best work itself | `tasks/<task>/archive/artifacts/<best session>.patch`: a diff against the task's baseline, to apply with `git apply` |
| what a session was told | `tasks/<task>/sessions/<sid>/input/prompt.md` and `archive.md` |
| what a session said it did and learned | `tasks/<task>/sessions/<sid>/derived/report.md` |
| what the agent actually did | `tasks/<task>/sessions/<sid>/raw/`, whatever the adapter keeps (the example keeps the agent's output and the evaluator's result) |
| why a starting point was chosen | `tasks/<task>/sessions/<sid>/input/selection.json` |
| everything that happened, in order | `events.jsonl` |
| tables and lineage graphs for analysis | `exports/` after `infinievolve export`: `nodes.csv`, `scores.csv`, `best_so_far.csv`, `<task>/lineage.{json,dot,mmd}` |

The session matrix of the example:

```
| session | parent | start | scored | valid | bytes | best so far | flags | agent min | wall min | failed |
|---|---|---|---|---|---|---|---|---|---|---|
| s000 | - | root | final | yes | 466 | 466 | - | - | 0.0 | - |
| s001 | s000 | best | final | yes | 286 | 286 | - | 0.0 | 0.0 | - |
| s002 | s001 | best | final | yes | 228 | 228 | - | 0.0 | 0.0 | - |
...
| s005 | s004 | best | final | yes | 163 | 163 | - | 0.0 | 0.0 | - |
| s006 | s005 | best | final | yes | 163 | 163 | duplicate (soft) | 0.0 | 0.0 | - |
```

Nothing is overwritten: every session keeps its inputs, its raw outputs and what was derived from them,
with a checksum of every file, so a result can be traced back and a different analysis can be run later.

## Using it on your own task

With the local adapter of the example you need no code beyond a script that scores a version.

1. **Copy `examples/local`** into your project. It holds the adapter, a task directory and a campaign.
2. **Describe the task** in `tasks/<name>/`:
   - `workspace/`: the baseline, that is, the files a session starts from;
   - a program that scores one version. It is run in a clean copy of the work and prints, as its last
     line, one JSON object: `{"checks": {"tests_pass": true}, "metrics": {"score": 0.82}, "reasons": []}`;
   - `task.yaml`:

     ```yaml
     instruction: |
       What the agent is asked to do.
     time_limit_seconds: 3600
     evaluate: ["{python}", "{task}/evaluate.py"]     # {task} is this directory
     declaration:
       metrics:
         score: {direction: max, scale: absolute, anchor: baseline}
       gate: [tests_pass]
       rank_by: score
     prompt:
       ranking: by `score`, higher being better, among versions that pass the tests.
     ```
3. **Point the campaign at your agent** in `campaigns/<name>/config.yaml`: set `tasks`, and set
   `options.agent_command` to a shell command that starts your agent without interaction and hands it the
   prompt. The command runs in a copy of the workspace with these set:

   | Variable | Is |
   |---|---|
   | `EVOLVE_PROMPT` | the path of the session prompt |
   | `EVOLVE_ARCHIVE` | the path of the full archive of earlier sessions |
   | `EVOLVE_WORKSPACE` | the working copy, also the current directory |
   | `EVOLVE_CAMPAIGN` | the campaign directory |

   Whatever the agent changes in that directory is its work, and whatever it prints is kept as the
   transcript the closing report is read from.
4. **Run it**: `infinievolve run campaigns/<name>`. It can be interrupted and run again.

An agent started this way edits files and runs commands without asking. Run it somewhere you would let
it do that.

Write an adapter of your own (see below) when sessions must run somewhere else than this machine, when
the work is not a set of file changes, or when you want to read the agent's usage, recognise a refused
agent, or check what it did during a session.

## How it works

Each task runs independently:

1. The unmodified baseline is scored once. It is the root of the lineage, `s000`.
2. Then, repeatedly:
   1. check the stop rule;
   2. choose the node the next session starts from (`best`, `explore` or `baseline`);
   3. write the session's prompt: the task, the starting point, a summary of the archive;
   4. run one agent session, then evaluate what it left behind;
   5. archive the result as a new node.
3. The task stops after `max_sessions` sessions, or when the last `window_k` sessions did not beat the
   best value by more than `min_improvement`.

Sessions of one task run one after another, because each reads the archive of those before it. Different
tasks run side by side, up to `max_parallel` jobs at once. A campaign can be interrupted at any point;
running it again re-attaches to jobs that are still going and resubmits what was prepared but not started.

The three ways to choose a starting point: `best` takes the valid node with the best ranking value;
`explore` draws among all valid nodes, more often the better ones and less often those many sessions
already started from, so a weaker node can still become a stepping stone; `baseline` always starts from
the unmodified baseline and uses the archive as context only.

## What you declare

An adapter gives a declaration for each task, and a campaign may override parts of it:

```yaml
metrics:                          # every number the evaluator reports
  pass_rate: {direction: max, scale: absolute, anchor: baseline, format: "{:.1%}"}
  tokens:    {direction: min, scale: absolute}
gate: [builds, tests_unchanged]   # the checks a version must all pass to be valid
rank_by: pass_rate                # the one metric that picks starting points and feeds the stop rule
show: [pass_rate, tokens]         # what later sessions see
confirm: {samples: 2, aggregate: median}   # score a new best again before trusting it (optional)
flags: {suspicious_access: hard}  # levels of the flags the adapter raises
artifact: patch                   # the form a session's work is archived in
```

- **direction** says which way is better; **scale** says whether differences (`absolute`) or quotients
  (`ratio`) are what compare, which decides how selection weighs candidates and how `min_improvement`
  is read (an amount, or a percentage); **anchor** says where the best-so-far curve starts.
- The evaluator reports named **checks** for each version it scores. A version is valid when every check
  in `gate` is true; a check that was not reported counts as failed.
- Ranking uses a single metric. Others are recorded, shown and exported.
- The declaration in force when the baseline is archived is fixed for that lineage. To change it,
  reset the task.

## What is in the archive

```
<campaign>/
  config.yaml  provenance.json  state.json  summary.{json,md}
  events.jsonl                       every state change, with a timestamp and the task
  tasks/<task>/
    task.json                        the declaration this lineage is fixed to
    sessions/<sid>/
      input/     prompt.md archive.md start.* selection.json spec.json
      raw/       whatever the adapter keeps (transcripts, logs, evaluator output)
      derived/   scores.json report.md stats.json artifacts/
      job.json node.json manifest.json (sha256 of every file)
    archive/     nodes/ artifacts/ archive.jsonl matrix.{json,md}    the view the next agent reads
  exports/       <task>/lineage.{json,dot,mmd}  nodes.csv scores.csv best_so_far.csv
```

Every node has the same skeleton, whatever the task: its place in the lineage, its archived work
(`kind`, `fingerprint`, `summary`), every scored version with its checks and metrics, whether it is
valid, the value it is ranked by, the agent's report, timing, usage, and its flags.

**Flags** say that a node's result should not be taken at face value. Each has a level: `soft` is
recorded and shown; `hard` keeps the node from being a starting point, a best, or part of the stop
curve; `void` says no session happened. The adapter raises flags and declares their levels; a person can
add one with `infinievolve flag` and lift them with `infinievolve clear`. The framework raises two of its
own: `duplicate` (soft) when a session's work is identical to an earlier node's, for artifact kinds that
can tell, and `confirmation_failed` (hard) when a repeat scoring rejects work the first scoring accepted.

**Artifact kinds** describe the form of a session's work. The adapter produces the bytes and restores
them as a later session's starting point; the kind summarises them for the prompt, renders them for the
full archive, and gives a fingerprint when equal bytes mean equal work. `patch` (a diff against the
baseline) and `text` (one document) are included; `infinievolve.artifacts.register_artifact` adds
another.

## Commands

A campaign is a directory whose `config.yaml` is its only configuration, and everything it produces
stays in that directory.

```sh
infinievolve new campaigns/first            # writes a commented config.yaml to edit
infinievolve run campaigns/first            # can be interrupted and run again
infinievolve status campaigns/first
infinievolve report campaigns/first [--task T]
infinievolve export campaigns/first
infinievolve reset campaigns/first --task T                        # start one task over
infinievolve flag  campaigns/first --task T --session S --flag NAME --reason "..." [--level hard|soft|void]
infinievolve clear campaigns/first --task T --session S --reason "..."
```

`prompt.carry` sets what a session inherits from the ones before it, independently of what it starts
from: `full` (the table, every report, every session's work), `reports` (without the work) or `none`
(no archive, and the prompt does not mention one).

## Writing an adapter

An adapter is the only code that knows your tasks. It implements `infinievolve.core.adapter.Adapter`:

| It says | With |
|---|---|
| what the tasks are | `resolve_tasks`, `instruction`, `default_time_limit_seconds` |
| what is recorded and ranked | `declaration` |
| the task-specific sentences of the prompt | `prompt_text` |
| how a job runs | `submit_baseline`, `submit_session`, `poll`, `collect` |
| whether the environment is broken | `baseline_problem` |
| how archived work is scored again, if it declares `confirm` | `submit_rescore` |

Jobs are asynchronous: `submit_*` returns a handle the adapter can find the job by after a restart,
`poll` says which jobs are over, `collect` returns the scored versions, their work, the report, timing
and any flags. Three exceptions tell the loop what a failure means: `JobFailed` (pause the task),
`AgentUnavailable` (the agent's service refused; wait, then resubmit) and `HostFault` (the place it ran
is broken; resubmit at once).

A campaign names its adapter as a registered name, an entry point of the group `infinievolve.adapters`,
`package.module:Name`, or `path/to/file.py:Name`.

[`examples/local`](examples/local) is a complete adapter in one file that runs every job as a local
process: copy it and change it. Where sessions run, how an agent is started and how accounts are managed
are deliberately left to adapters.

The prompt is assembled from three sources, so that the framework's own text says nothing about any kind
of task: the framework's skeleton (how a session works, the archive, the closing report), the
declaration (the table's columns, the report's fields) and the adapter's `PromptText`.

## Tests

```sh
uv pip install -p .venv pytest && .venv/bin/python -m pytest -q
```

`tests/fake_adapter.py` stands in for an adapter without running anything; one test runs the local
example end to end and needs `git`.

## License

[MIT](LICENSE)
