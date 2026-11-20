English | [简体中文](timing-tasks.zh-CN.md)

# Tasks ranked by speed

A timing is noisy. The same code gives numbers several percent apart on another machine, at another
moment, or twice in a row. In one real campaign, six scorings of one unchanged piece of work spread over
more than 7%: wide enough for a search that trusts single timings to crown a copy of existing work as its
new best, and to start every later session from it.

`infinievolve.task_types.perf` holds what every benchmark of this kind can share against that. It sits
between the framework, which knows nothing about any kind of task, and the adapter of one benchmark.

## The layers

| Layer | Answers | Shared by |
|---|---|---|
| framework (`infinievolve.core`) | how to search and what to keep | every task |
| task type (`infinievolve.task_types.perf`) | how a result of this kind is measured so that it can be trusted, and how the agent is told | every benchmark of the kind |
| adapter | where tasks come from and how one is evaluated | every task of one benchmark |
| your config | where it runs and as whom | you |

## What is there

**In the framework, for any noisy metric**

- *Repeat scoring.* A new best is scored again and ranked by the median (`confirm` in the declaration).
- *Shared measurements.* Sessions that end with identical work are one piece of work measured several
  times: each is ranked by the aggregate of all those measurements.
- *Scoring again on request.* An adapter that does not trust a measurement says so
  (`SessionResult.remeasure`), and the work is scored again, elsewhere where the adapter can arrange it.
- *The reference.* The baseline when it is valid, else the first valid session. Its work is handed to
  every job, for adapters that measure against it.

**In the task type, for timing**

| | What it does | Default |
|---|---|---|
| paired timing | times the candidate and a reference in alternation on one machine, in a balanced order, and ranks by the ratio | on, where a task can be paired |
| snapshot and check | times a private copy of the work, and discards the timing if that copy changed meanwhile | on |
| exact measures | a task whose "speed" is a count (instructions, bytes, operations) is neither paired nor scored again | on |
| one hardware model | measurements taken on another model of processor are taken again | off: name the model to turn it on |
| machine ledger | a machine whose measurement did not hold up elsewhere gets a strike; two strikes and it is not used again | off: name a ledger file to turn it on |
| wall clock | makes a workload that reads CPU time read the wall clock instead | off |
| quiet machine | ends what the agent or an earlier timing left running before the next timing | off: it kills processes |

The last four change what is measured or act on your machines, so they are yours to turn on.

## When none of this applies

Pairing needs two versions that can both be run again, side by side, on a metric that varies. It has no
place where the metric is exact, where an evaluation cannot be repeated (a held-out score read once, a
run that takes hours), or where two runs do not see the same world (live data, an external service).
Such tasks use the framework's repeat scoring if anything, or nothing at all.

## Writing an adapter for a timing benchmark

Subclass `PerfAdapter` and say what your benchmark is like; the declaration and the prompt's wording
follow from it.

```python
from infinievolve.task_types.perf.adapter import PerfAdapter

class MyBench(PerfAdapter):
    gate = ("correct", "in_scope")

    def pairs_with(self, task):      # what a paired timing is taken against; None: cannot be paired
        return "the task's reference solution"

    def exact(self, task):           # True: the measure is a count, the same every time
        return False

    def hardware(self, task):
        return "8 CPU cores"

    # ... and the job methods of the adapter interface
```

In `collect`, build each scored version with `timing_row(checks=..., speedup=..., pairing=...)`. It
returns the score row and, when the pairing's blocks disagree too much or the timing did not complete,
the reason to hand back as `remeasure`.

The measurement itself runs where your jobs run. `measure.py` is one file of standard library only: send
it along with the script your jobs execute, and call `run_blocks` with two functions that each time one
arm once. The same file computes the statistics on both sides.

[`examples/faibench`](../examples/faibench) is a complete adapter built this way.
