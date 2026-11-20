"""The layer shared by tasks ranked by how fast something runs."""
from __future__ import annotations

import math
from pathlib import Path
import subprocess
import sys

import pytest

from infinievolve.core.schema import parse_declaration
from infinievolve.task_types.perf import measure, policy
from infinievolve.task_types.perf.adapter import PerfAdapter
from infinievolve.task_types.perf.hosts import HostLedger


def test_blocks_alternate_in_a_balanced_order_and_stop_at_the_budget():
    assert measure.orders(6) == ["AB", "BA", "BA", "AB", "AB", "BA"]
    calls, clock = [], {"now": 0.0}

    def arm(name, cost):
        def time_it():
            calls.append(name)
            clock["now"] += cost
            return cost
        return time_it

    blocks = measure.run_blocks(arm("candidate", 10), arm("reference", 30), blocks=4, budget_seconds=1000,
                                clock=lambda: clock["now"])
    assert calls == ["reference", "candidate", "candidate", "reference", "candidate", "reference", "reference", "candidate"]
    assert blocks == [{"reference": 30.0, "candidate": 10.0}] * 4
    calls.clear()
    clock["now"] = 0.0
    assert len(measure.run_blocks(arm("candidate", 10), arm("reference", 30), blocks=4, budget_seconds=50,
                                  clock=lambda: clock["now"])) == 2      # a block is never cut in half


def test_the_paired_ratio_ignores_what_the_machine_added_to_both_arms():
    quiet = [{"reference": 100, "candidate": 50}] * 4
    busy = [{"reference": 100 * load, "candidate": 50 * load} for load in (1.0, 1.7, 0.9, 2.4)]   # same code, a busy machine
    assert measure.paired(quiet)["ratio"] == pytest.approx(2.0) and measure.paired(busy)["ratio"] == pytest.approx(2.0)
    assert measure.paired(busy)["dispersion"] == pytest.approx(0.0)
    noisy = measure.paired([{"reference": 100, "candidate": 50}, {"reference": 100, "candidate": 25},
                            {"reference": 100, "candidate": 100}])
    assert noisy["ratio"] == pytest.approx(2.0) and noisy["dispersion"] > 0.5 and noisy["blocks"] == 3
    assert measure.paired([{"reference": 0, "candidate": 5}]) == {"ratio": None, "dispersion": None, "blocks": 0}
    assert measure.pooled([3.0, 3.4, 3.1, 3.3, 3.0, 3.2]) == pytest.approx(3.15)
    assert measure.pooled([]) is None and measure.pooled([1, 3], "mean") == 2


def test_the_measurement_file_stands_alone():
    """It is sent to the machines jobs run on, where this package is not installed."""
    source = Path(measure.__file__).read_text()
    assert "infinievolve" not in source.replace("INFINIEVOLVE_JOB_TAG", "") and "import yaml" not in source
    done = subprocess.run([sys.executable, "-c", "import measure; print(measure.paired([{'reference': 8, 'candidate': 2}])['ratio'])"],
                          cwd=Path(measure.__file__).parent, capture_output=True, text=True)
    assert done.stdout.strip() == "4.0"


def test_the_shim_makes_a_workload_time_the_wall_clock(tmp_path):
    script = tmp_path / "workload.py"
    script.write_text("import sys, time\nstart = time.process_time(); time.sleep(0.2)\n"
                      "print(sys.argv[1], round(time.process_time() - start, 1))\n")
    plain = subprocess.run([sys.executable, str(script), "plain"], capture_output=True, text=True).stdout.split()
    shimmed = subprocess.run([sys.executable, "-c", measure.WALLCLOCK, str(script), "shimmed"], capture_output=True,
                             text=True).stdout.split()
    assert plain == ["plain", "0.0"] and shimmed == ["shimmed", "0.2"]      # sleeping costs no CPU time


def test_a_snapshot_is_checked_before_and_after(tmp_path):
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "a.py").write_text("x = 1\n")
    (tree / ".git").mkdir()
    (tree / ".git" / "HEAD").write_text("ref\n")
    copy = measure.snapshot(tree, tmp_path / "copy")
    before = measure.digest(copy)
    assert list(before) == ["pkg/a.py"]                        # version-control internals are not the work
    (tree / "pkg" / "a.py").write_text("x = 2\n")             # the live tree moving on does not touch the copy
    assert measure.digest(copy) == before
    (copy / "pkg" / "a.py").write_text("x = 3\n")
    assert measure.digest(copy) != before
    assert measure.digest(copy, ["pkg/a.py", "missing.py"])["missing.py"] is None


@pytest.mark.skipif(not Path("/proc/self/environ").is_file(), reason="needs /proc")
def test_only_processes_of_this_job_are_ended():
    import os
    tag = measure.tag_job()
    ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    theirs = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              env={key: value for key, value in os.environ.items() if key != measure.JOB_TAG})
    try:
        ended = measure.reap_tagged(tag)
        assert [item["pid"] for item in ended] == [ours.pid] and ours.wait(timeout=10) != 0
        assert theirs.poll() is None                           # untagged: not this job's, left alone
    finally:
        theirs.kill()
        os.environ.pop(measure.JOB_TAG, None)


def test_a_machine_is_dropped_on_its_second_unreproduced_measurement(tmp_path):
    ledger = HostLedger(tmp_path / "hosts.yaml")
    assert ledger.excluded() == []
    assert ledger.strike("node7", "blocks disagree by 80%", {"session": "s003"}) == "strike"
    assert ledger.strike("node7", "blocks disagree by 80%", {"session": "s003"}) is None      # the same measurement
    assert ledger.excluded() == []
    assert ledger.strike("node7", "ten times off", {"session": "s009"}) == "excluded"
    assert ledger.excluded() == ["node7"] and ledger.strike("node7", "again", {"session": "s011"}) is None
    assert ledger.exclude("node2", "its accelerator does not initialise") and not ledger.exclude("node2", "again")
    assert HostLedger(tmp_path / "hosts.yaml").excluded() == ["node7", "node2"]


def test_what_a_timing_task_declares():
    paired = parse_declaration(policy.declaration(paired=True, gate=["correct"]))
    assert paired.rank_by == "paired_speedup" and paired.show == ("paired_speedup", "speedup")
    assert paired.confirm.samples == 2 and paired.rank.scale == "ratio"
    alone = parse_declaration(policy.declaration(paired=False, gate=["correct"], samples=3))
    assert alone.rank_by == "speedup" and alone.confirm.samples == 3 and "paired_speedup" not in alone.metrics
    exact = parse_declaration(policy.declaration(paired=True, gate=["correct"], exact=True))
    assert exact.rank_by == "speedup" and exact.confirm is None and "paired_speedup" not in exact.metrics


def test_when_a_measurement_is_not_believed():
    assert policy.distrust(None) is None and policy.distrust({"ratio": 2.0, "dispersion": 0.04, "blocks": 4}) is None
    assert "disagree by 80%" in policy.distrust({"ratio": 2.0, "dispersion": 0.8, "blocks": 4})
    assert "did not complete" in policy.distrust({"status": "candidate_changed"})
    assert policy.same_hardware("Model A", "Model A") is None and policy.same_hardware(None, "Model B") is None
    assert "Model B" in policy.same_hardware("Model A", "Model B")


def test_an_adapter_of_a_timing_task_inherits_the_rest():
    class Mine(PerfAdapter):
        gate = ("correct", "in_scope")

        def pairs_with(self, task):
            return "the task's reference solution" if task == "timed" else None

        def exact(self, task):
            return task == "counted"

        def hardware(self, task):
            return "8 CPU cores"

    adapter = Mine()
    assert parse_declaration(adapter.declaration("timed")).rank_by == "paired_speedup"
    assert parse_declaration(adapter.declaration("counted")).confirm is None
    timed, counted = adapter.prompt_text("timed"), adapter.prompt_text("counted")
    assert "timed in alternation on the same machine, with 8 CPU cores available" in timed.ranking
    assert "the task's reference solution" in timed.ranking and "2 more time(s)" in timed.ranking
    assert "alternation" not in counted.ranking and "more time(s)" not in counted.ranking
    assert timed.goal == "You are optimising the task below."
    row, again = adapter.timing_row(checks={"correct": True}, speedup=120.0,
                                    pairing={"ratio": 1.4, "dispersion": 0.9, "blocks": 4})
    assert row.metrics == {"speedup": 120.0, "paired_speedup": 1.4, "paired_dispersion": 0.9}
    assert "disagree by 90%" in again and not math.isnan(row.metrics["paired_speedup"])
    row, again = adapter.timing_row(checks={"correct": False}, speedup=None)
    assert row.metrics == {"speedup": None} and again is None
