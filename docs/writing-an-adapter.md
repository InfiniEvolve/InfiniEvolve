English | [简体中文](writing-an-adapter.zh-CN.md)

# Writing an adapter

An adapter is the one piece of code that knows your tasks: what they are, where a session runs, how its
work is evaluated, and how that work is captured and restored. This page says exactly what the framework
gives an adapter and what it expects back.

The built-in adapter, [`infinievolve/local.py`](../infinievolve/local.py), is a complete one in a single
file: every job is a process on this machine. Read it beside this page.
[`examples/faibench`](../examples/faibench) is one whose jobs run on a cluster, over ssh.

## How an adapter is made

`campaign.adapter` names a callable. It is called once per `infinievolve` command with the campaign's
`CampaignConfig`, and returns the adapter object. A class whose `__init__` takes the config is the usual
form.

What the config holds for you:

| Attribute | Is |
|---|---|
| `config.name` | the campaign's name (its directory name) |
| `config.root` | the campaign directory, absolute |
| `config.options` | the `options:` section, as it is |
| `config.defaults.agent` | the `agent:` section, as it is |
| `config.for_task(task)` | that task's settings: `carry`, `session_minutes`, `hide_scores`, ... |
| `config.max_parallel`, `config.poll_seconds` | the scheduler's limits |

The framework reads nothing in `options:` or `agent:`. Check them in `__init__` and raise `ValueError`
for what is missing, so a wrong config fails before any job.

An adapter object lives for one command. Keep nothing in memory that a later `run` needs: put it in the
job handle or on disk.

### An adapter in several files

`path/to/file.py:Name` loads that one file. If its directory has an `__init__.py`, the directory is loaded
as a package, so `from .other import thing` works. Otherwise keep the adapter in one file, and read
anything else beside it through `Path(__file__).parent`.

## The life of one session

```
framework                              adapter                           on disk: sessions/<sid>/
---------                              -------                           ------------------------
chooses the start node
writes the prompt and the archive                                        input/prompt.md
                                                                         input/archive.md      (unless carry: none)
                                                                         input/start<suffix>   (unless from the baseline)
                                                                         input/reference<suffix> (see below)
                                                                         input/selection.json, input/spec.json
calls submit_session(spec, dir)  --->  starts the job, returns a handle  input/...  anything of your own
records the handle                                                       job.json
calls poll(jobs) until it is over --->  says which jobs are over
calls collect(job, dir)          --->  fetches outputs, returns          raw/...    everything you keep
                                       a SessionResult
archives the result                                                      derived/scores.json, report.md, artifacts/
                                                                         node.json, manifest.json
```

`input/` is the framework's, plus whatever you add for your own job. `raw/` is yours entirely: keep the
transcript, logs and evaluator output there, since nothing else records them. `derived/`, `job.json`,
`node.json` and `manifest.json` are the framework's; do not write them.

A session's work reaches you as bytes: `spec.start_artifact` (also on disk as `input/start<suffix>`, the
suffix being the artifact kind's, `.patch` for the default kind). It is exactly what you returned as that
node's artifact, so restoring it is the reverse of however you captured it. `None` means "start from the
baseline".

### The reference

Some measurements only mean something against another version measured the same way, at the same time,
in the same place: timing a candidate in alternation with a reference is the usual case. For that, the
framework puts the work of the campaign's reference node beside a job's inputs, as
`input/reference<suffix>`, for sessions and for confirm jobs alike. The reference is the baseline when it
is valid and the first valid session otherwise; no file is written while it is the baseline (you have
that already) or while nothing is valid yet. Using it is up to you: most evaluations need no reference,
and not every kind of task can be measured in pairs at all.

## The methods

### Describing tasks

| Method | Called | Returns |
|---|---|---|
| `resolve_tasks(selection)` | once per command | the task names: the list as given after checking it, or every task for `"all"` |
| `declaration(task)` | before the baseline, and whenever the loop checks it | the declaration (see the README); it is fixed once the baseline is archived |
| `instruction(task)` | for each prompt | the task statement, pasted into the prompt verbatim |
| `prompt_text(task)` | for each prompt | a `PromptText` (below) |
| `default_time_limit_seconds(task)` | for each session, unless the config sets `session_minutes` | the limit your job should enforce |
| `baseline_problem(task, root)` | before each session | why the baseline's result shows a broken environment, else `None` |
| `provenance(tasks)` | once, at the first run | versions and digests worth pinning; never secrets |

`archive_location` is an attribute, not a method: a phrase saying where the agent can read the full
archive, pasted into the prompt as "... is at *archive_location* (read-only)". Make `input/archive.md`
readable at that place when you start the agent.

`baseline_problem` gets the root `Node`. Its `scores` are dictionaries with the fields of `ScoreRow`
(`checks`, `metrics`, `reasons`). A baseline that fails the gate is normal for a task that starts from a
stub; report a problem only when the result cannot come from the task itself.

The time limit is yours to enforce. The framework passes it in `spec.time_limit_seconds` and never tells
the agent about it.

### Running jobs

| Method | Does |
|---|---|
| `submit_baseline(task, session_dir, events)` | starts scoring the untouched baseline; returns a handle |
| `submit_session(spec, session_dir, events)` | starts one agent session; returns a handle |
| `submit_rescore(task, name, artifact, job_dir, events, avoid=())` | starts scoring archived work again, without an agent. Needed only when the declaration sets `confirm` |
| `poll(jobs, events)` | returns the `job.key` of every job that is over |
| `collect(job, session_dir, events)` | fetches a finished job's outputs and returns a `SessionResult` |

**The handle** is a dictionary of your own, saved as JSON in `job.json`. It must be enough to find the
job again from a new process: a process id and a directory, a cluster's job id and a remote path. Use
only JSON types.

**`poll`** gets every job in flight at once, so one query can cover them all. Return a job only when you
know it is over; one you could not find out about is not over. If `poll` raises, the scheduler logs it
once, keeps the jobs and tries again at the next interval, so a dropped connection needs no handling.
A job that ended without a result is over too: `collect` is where you say so.

**`job.kind`** is `"baseline"`, `"session"` or `"confirm"`. A confirm job's `session_id` is
`<sid>-c<n>` (`s003-c1`), the same `name` its `submit_rescore` got, and its directory is
`sessions/<sid>/confirm/r<n>/`, laid out like a session's: your `input/` and `raw/` go there.
`avoid` lists the `timing["where"]` of the earlier scorings of that work, for adapters that can choose
to run elsewhere.

**The scheduler runs on your machine** for as long as the campaign does. Jobs on a cluster keep running
when it stops; the next `run` re-attaches to them through their handles.

## What `collect` returns

```python
SessionResult(
    scores=[ScoreRow(label="final", checks={...}, metrics={...}, reasons=[...], extra={...})],
    artifacts={"final": b"..."},
    report="...", timing={...}, usage={...}, stats={...}, flags={...}, error=None)
```

**`scores`**: one `ScoreRow` per version you evaluated. Most adapters evaluate one, the state the session
ended in, and label it `"final"`. If the task scores intermediate submissions too, return a row for each
under labels of your own; the framework archives the best valid one by the ranking metric.

- `checks`: `{name: bool}`. The declaration's `gate` lists the ones a version must pass; a missing one
  counts as failed. Without a declared gate, every check you report must be true. Report every check you know about, gated or not: they are recorded either way.
- `metrics`: `{name: number}` for the metrics the declaration names. Leave out or pass `None` what was
  not measured. A valid version without a value for the ranking metric is archived but is not a
  starting point.
- `reasons`: why checks failed, in the evaluator's words; shown to later sessions.
- `extra`: anything else about this scoring worth keeping.

**`artifacts`**: `{label: bytes}`, the work of each scored version in the form of the declared artifact
kind. For the default kind that is a diff of the version against the baseline, new files included, which
`git apply` can restore. Return the baseline's as empty bytes.

**`report`**: the agent's closing report. `infinievolve.core.prompt.extract_report(texts)` finds it in
what the agent said (one text, or its messages in order). Turning a transcript into those texts is your
part, since every agent writes its own format.

**`timing`**: seconds, by whatever names you like. The framework reads three and keeps the rest:

| Key | Used for |
|---|---|
| `agent_seconds` | the "agent min" column the next sessions and the tables show |
| `queue_seconds` | the scheduler's log line |
| `where` | the place the job ran, handed back as `avoid` to `submit_rescore` |

The framework adds `wall_seconds`, from preparing the session to archiving it.

**`usage`**: tokens and cost, as the agent reports them. **`stats`**: anything else per session. Both are
stored on the node and exported as they are.

**`flags`**: `{name: reason}` for results that should not be taken at face value, such as a session that
read the answers. The declaration's `flags` gives each name a level (`hard` unless it says otherwise).

**`error`**: a note on the node for something wrong that still left a result worth archiving.

**`remeasure`**: a reason, when you do not trust this measurement (a timing whose runs disagree, a result
from the wrong hardware). The work is then scored again with `submit_rescore`, at least twice, and ranked
by the aggregate. Leave it `None` otherwise.

For a baseline or a confirm job, `scores` and `artifacts` (and `timing`) are all that is read.

## When a job goes wrong

Raise from `collect`:

| Raise | When | The loop then |
|---|---|---|
| `JobFailed` | the job broke before producing a result | pauses the task; the next `run` resubmits the same session |
| `AgentUnavailable(message, retry_at)` | the agent's service refused it before it worked (a limit, authentication, an outage) | holds every task until `retry_at`, then resubmits |
| `HostFault` | the place it ran is broken, and you have arranged not to use it again | resubmits at once |

Before raising any of them, delete what the attempt left in `raw/` (or move it aside): the session is
resubmitted with the same inputs, and nothing of a failed attempt may be mistaken for its result.

Any other exception from `collect` is taken as "could not fetch": the task pauses, the job stays
recorded, and the next `run` collects it again. An exception from a `submit_*` method pauses the task
before anything is recorded, except `SubmitDeferred`.

A submit (`submit_baseline`, `submit_session`, `submit_rescore`) may refuse on purpose, when the task's
next job must wait for something outside the campaign: a round gate an operator opens, a quota window.
Raise `SubmitDeferred(message, retry_after=None)` then. The task is not paused and `run` does not exit
while it waits: the same submit is tried again every `retry_after` seconds (5 minutes when `None`) while
the other tasks go on, and `submit_deferred` / `submit_resumed` events mark when the wait starts and
ends. Any other exception from a submit pauses the task until the next `run`.

A session the agent cut short, or that produced invalid work, is not a failure: return its result.

## Events

`events.emit(type, session_id, **data)` adds a line to the campaign's `events.jsonl`. Emitting is
optional. An adapter may use `job_submitted`, `job_started`, `job_held`, `job_finished`, `collected` and
`host_excluded`, or any type of its own that starts with `adapter_`; other names are the framework's and
are refused. In `poll`, bind the log to the job's task first: `events.for_task(job.task).emit(...)`.

## The prompt's task-specific sentences

`PromptText` fills the places of the prompt the framework cannot write:

| Field | Appears as | Default |
|---|---|---|
| `goal` | the prompt's first sentence | "You are working on the task below." |
| `working` | first rule: how to work so the result can be captured | "Do your work in the directory you are started in." |
| `ranking` | "How your work is ranked: ..." | said from the declaration: the ranking metric, which way is better, the gate, repeat scoring |
| `feedback` | a rule of its own, when set: how to check work during the session | left out |
| `persistence` | after "You decide when the session is over." | keep going while a promising idea is left |
| `continuing` | after the starting point, when it is an earlier session's work | improve on it and keep it valid |

Say in `ranking` what is measured, on what, and how noisy it is. Say in `working` whatever your capture
depends on: if you take a diff against a commit, tell the agent not to commit.

## Testing an adapter

Start with the baseline alone: set `stop.max_sessions` to `1` and run. The first job scores the untouched
baseline without an agent, and exercises submit, poll, collect and your evaluator. Its line in the log
should show a valid result with the value you expect.

Then check the round trip that only a second session exercises: the work captured from one session,
restored as the start of the next. Capture, restore into a fresh copy, evaluate, and compare the score.
The test of the bundled example does this.

`tests/fake_adapter.py` shows the smallest adapter there is: no jobs, every result scripted. It is the
quickest way to try a declaration or a config against the real loop.

## Checklist

- `__init__` refuses a config that lacks what the adapter needs.
- The handle alone finds a job from a new process.
- `poll` never reports a job it could not check.
- The evaluation runs on the captured work, not on whatever the agent left around it.
- A restored artifact scores the same as when it was captured.
- `collect` clears `raw/` before raising `JobFailed`, `AgentUnavailable` or `HostFault`.
- Nothing site-specific or secret is in `provenance`, in events, or in the code.
