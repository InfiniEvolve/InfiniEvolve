# A fai-bench adapter

An adapter for fai-bench task packages on an HTCondor cluster reached over ssh, with the agent running
inside each task's Apptainer image. It is the worked example of two things:

- an adapter whose jobs run somewhere else than this machine ([Writing an adapter](../../docs/writing-an-adapter.md));
- a benchmark ranked by speed, built on `infinievolve.task_types.perf`
  ([Tasks ranked by speed](../../docs/timing-tasks.md)): paired timing against the task's reference
  solution, repeat scoring of a new best, shared measurements of identical work.

```
adapter.py            the adapter: what the tasks are, submitting, polling, collecting
job.py                what one job runs on a node; standard library only, sent along with every job
config.example.yaml   a campaign config to copy and fill in
```

`measure.py` from `infinievolve.task_types.perf` is sent along with `job.py`, so the node and the
scheduler compute the same statistics.

## Using it

You need a checkout of the task packages, their prebuilt images on the cluster, `ssh` and `rsync` access
to a machine that can submit HTCondor jobs, and a Claude Code binary and token file readable from the
nodes.

```sh
mkdir -p campaigns/first && cp examples/faibench/config.example.yaml campaigns/first/config.yaml
# fill in the paths and the host
infinievolve check campaigns/first --prompt
infinievolve run campaigns/first
```

Keep the config out of public repositories: it names your hosts, paths and credential file.

## What is measured, and what is yours to turn on

On by default: paired timing where a task has a timing workload, a snapshot of the tree being timed that
is checked before and after, repeat scoring of a new best, and no pairing or repeat scoring for tasks
whose measure is a count.

Off unless the config asks: a ClassAd requirement for every job, a processor model that measurements must
come from, a ledger of machines whose measurements did not hold up, timing by the wall clock, and ending
leftover processes between steps. The last one kills processes; it only ever touches ones the job itself
started. `adapter.py` lists the settings.

## What it leaves out

Tasks that write outside `/app/repo`; the tasks' sealed development loop (the agent's container can read
what the image holds, so set `forbidden` patterns to flag sessions that look where they should not); GPU
health checks; more than one account. It is an example of the interface, not a replacement for the
benchmark's own runner.
