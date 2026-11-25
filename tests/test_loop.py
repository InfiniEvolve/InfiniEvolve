"""The loop and the archive, driven by the stand-in adapter."""
from __future__ import annotations

import json

import pytest

from infinievolve import artifacts
from infinievolve.adapters import make_adapter
from infinievolve.config import parse_config
from infinievolve.core.archive import CampaignStore
from infinievolve.core.loop import Scheduler

from . import fake_adapter  # noqa: F401  (registers "fake")


def campaign(tmp_path, values, *, stop=None, strategy=None, prompt=None, evaluation=None, declaration=None,
             rescore=None, overrides=None):
    data = {"campaign": {"adapter": "fake", "tasks": "all", "poll_seconds": 0},
            "agent": {"program": "none"},
            "stop": {"max_sessions": 6, "window_k": 2, "min_improvement": 5, **(stop or {})},
            "strategy": strategy or {}, "prompt": prompt or {}, "evaluation": evaluation or {},
            "task_overrides": overrides or {},
            "options": {"values": values, "declaration": declaration or {}, "rescore": rescore or {}}}
    config = parse_config(data, name="test", root=tmp_path)
    (tmp_path / "config.yaml").write_text("{}")
    return CampaignStore(tmp_path), config


def run(store, config, adapter=None, **keywords):
    adapter = adapter or make_adapter(config)
    scheduler = Scheduler(store, config, adapter, log=lambda line: None, sleep=lambda seconds: None, **keywords)
    return scheduler.run(), adapter


def nodes(store, task="a"):
    return {node.session_id: node for node in store.task(task).nodes()}


def events(store, kind):
    return [event for event in store.events.read() if event["type"] == kind]


def test_a_lineage_runs_until_the_window_brings_nothing(tmp_path):
    store, config = campaign(tmp_path, {"a": [1.0, 2.0, 3.0, 3.0, 3.1]})
    state, _ = run(store, config)
    archived = nodes(store)
    assert list(archived) == ["s000", "s001", "s002", "s003", "s004"]
    assert [archived[sid].parent_id for sid in ("s001", "s002", "s003", "s004")] == ["s000", "s001", "s002", "s002"]
    # 3.1 is within 5% of the 3.0 of two sessions earlier
    assert state["tasks"]["a"]["stopped"] and "within 5.0%" in state["tasks"]["a"]["stop_reason"]
    assert state["tasks"]["a"]["best_curve"] == [1.0, 2.0, 3.0, 3.0, 3.1]
    assert (tmp_path / "tasks" / "a" / "archive" / "matrix.md").is_file()
    assert json.loads((tmp_path / "tasks" / "a" / "task.json").read_text())["declaration"]["rank_by"] == "score"


def test_a_metric_where_lower_is_better_and_differences_count(tmp_path):
    declaration = {"metrics": {"score": {"direction": "min", "scale": "absolute"}}}
    store, config = campaign(tmp_path, {"a": [100, 80, 90, 70, 69, 69]}, declaration=declaration,
                             stop={"min_improvement": 2})
    state, _ = run(store, config)
    archived = nodes(store)
    assert archived["s003"].parent_id == "s001"        # 80 beats 90
    assert archived["s004"].parent_id == "s003"
    assert state["tasks"]["a"]["best"] == 69
    assert state["tasks"]["a"]["best_curve"] == [100, 80, 80, 70, 69, 69]
    assert "within 2.0 of" in state["tasks"]["a"]["stop_reason"]   # an amount, not a percentage


def test_a_version_must_pass_every_gated_check(tmp_path):
    values = {"a": [1.0, {"value": 9.0, "checks": {"ok": False}}, {"value": 8.0, "checks": {}}, 2.0, 2.0, 2.0]}
    store, config = campaign(tmp_path, values, stop={"window_k": 3})
    run(store, config)
    archived = nodes(store)
    assert not archived["s001"].valid and not archived["s002"].valid   # failed, and not reported
    assert archived["s003"].parent_id == "s000"                         # neither became a starting point
    assert archived["s004"].parent_id == "s003"


def test_the_campaign_can_tighten_the_gate(tmp_path):
    values = {"a": [1.0, {"value": 5.0, "checks": {"ok": True, "tidy": False}}, 2.0, 2.0, 2.0]}
    store, config = campaign(tmp_path, values, evaluation={"gate": ["ok", "tidy"]})
    run(store, config)
    assert not nodes(store)["s001"].valid
    assert nodes(store)["s000"].valid is False     # the baseline reports no `tidy` either: not valid, not an error


def test_flag_levels_come_from_the_declaration(tmp_path):
    values = {"a": [1.0, {"value": 9.0, "flags": {"cheated": "read the answers"}},
                    {"value": 5.0, "flags": {"odd": "worth a look"}}, 5.0, 5.0]}
    store, config = campaign(tmp_path, values, declaration={"flags": {"odd": "soft"}})
    state, _ = run(store, config)
    archived = nodes(store)
    assert archived["s001"].flags["cheated"]["level"] == "hard" and archived["s001"].excluded
    assert archived["s002"].flags["odd"]["level"] == "soft" and not archived["s002"].excluded
    assert archived["s002"].parent_id == "s000"      # the flagged 9.0 is not a starting point
    assert state["tasks"]["a"]["best_session"] == "s002"
    prompt = (tmp_path / "tasks" / "a" / "sessions" / "s003" / "input" / "prompt.md").read_text()
    assert "Not ranked and not a starting point: read the answers." in prompt


def test_the_same_work_twice_is_noted_when_the_kind_can_tell(tmp_path):
    values = {"a": [1.0, {"value": 2.0, "work": b"same"}, {"value": 2.6, "work": b"same"}, 2.6, 2.6]}
    store, config = campaign(tmp_path, values)
    run(store, config)
    assert nodes(store)["s002"].flags["duplicate"]["level"] == "soft"
    assert not nodes(store)["s002"].excluded


def test_no_duplicates_without_a_fingerprint(tmp_path):
    class Opaque(artifacts.Text):
        name = "opaque"

        def fingerprint(self, data):
            return None

    artifacts.register_artifact(Opaque())
    values = {"a": [1.0, {"value": 2.0, "work": b"same"}, {"value": 2.6, "work": b"same"}, 2.6, 2.6]}
    store, config = campaign(tmp_path, values, declaration={"artifact": "opaque"})
    run(store, config)
    assert all("duplicate" not in node.flags for node in nodes(store).values())
    assert nodes(store)["s002"].artifact == {"kind": "opaque", "fingerprint": None,
                                             "summary": {"characters": 4, "lines": 1}}


def test_a_new_best_is_scored_again_and_ranked_by_the_median(tmp_path):
    store, config = campaign(tmp_path, {"a": [1.0, 4.0, 2.5, 2.5]}, rescore={"a": [2.0, 3.0]},
                             evaluation={"confirm": {"samples": 2, "aggregate": "median"}})
    state, adapter = run(store, config)
    archived = nodes(store)
    assert adapter.rescored == {"a": 2}                 # only s001 was a new best
    assert archived["s001"].rank_value == 3.0           # median of 4.0, 2.0, 3.0
    assert archived["s001"].chosen()["metrics"]["score"] == 4.0   # what the session measured is kept
    assert state["tasks"]["a"]["best"] == 3.0
    assert events(store, "rescored")[0]["data"]["values"] == [4.0, 2.0, 3.0]


def test_a_repeat_scoring_that_fails_the_gate_takes_the_node_out(tmp_path):
    store, config = campaign(tmp_path, {"a": [1.0, 4.0, 2.0, 2.0, 2.0]},
                             rescore={"a": [{"value": 4.0, "checks": {"ok": False}}, 2.0]},
                             evaluation={"confirm": {"samples": 1}})
    run(store, config)
    archived = nodes(store)
    assert archived["s001"].flags["confirmation_failed"]["level"] == "hard"
    assert archived["s002"].parent_id == "s000"


def test_confirm_needs_an_adapter_that_can_score_again(tmp_path, monkeypatch):
    store, config = campaign(tmp_path, {"a": [1.0, 2.0]}, evaluation={"confirm": {"samples": 1}})
    monkeypatch.delattr(fake_adapter.FakeAdapter, "submit_rescore")
    state, _ = run(store, config)
    assert "submit_rescore" in state["paused"]["a"]
    assert nodes(store) == {}


def test_a_refused_agent_holds_the_campaign_and_the_session_is_resubmitted(tmp_path):
    values = {"a": [1.0, {"raise": "unavailable", "retry_at": 1000.0, "then": 2.0}, 2.0, 2.0]}
    store, config = campaign(tmp_path, values)
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += max(seconds, 1.0)

    adapter = make_adapter(config)
    Scheduler(store, config, adapter, log=lambda line: None, sleep=sleep, clock=lambda: clock["now"]).run()
    assert clock["now"] >= 1060                                   # waited past retry_at
    assert [spec.session_id for spec in adapter.submitted][:2] == ["s001", "s001"]
    assert adapter.submitted[0].prompt == adapter.submitted[1].prompt
    assert nodes(store)["s001"].rank_value == 2.0
    assert len(events(store, "agent_unavailable")) == 1 and len(events(store, "campaign_hold")) == 1


def test_a_failed_job_pauses_the_task_and_the_next_run_resubmits_it(tmp_path):
    values = {"a": [1.0, {"raise": "failed", "then": 2.0}, 2.0, 2.0], "b": [1.0, 1.0, 1.0]}
    store, config = campaign(tmp_path, values)
    adapter = make_adapter(config)
    state, _ = run(store, config, adapter)
    assert "job failed" in state["paused"]["a"] and state["tasks"]["b"]["stopped"]   # b was not held back
    state, _ = run(store, config, adapter)
    assert not state["paused"] and state["tasks"]["a"]["stopped"]
    assert nodes(store)["s001"].rank_value == 2.0
    first, second = [spec for spec in adapter.submitted if spec.task == "a" and spec.session_id == "s001"]
    assert first.prompt == second.prompt and first.parent_id == second.parent_id


def test_what_a_session_inherits(tmp_path):
    values = {"a": [1.0, 2.0, 3.0, 3.0, 3.0], "b": [1.0, 2.0, 3.0, 3.0, 3.0], "c": [1.0, 2.0, 3.0, 3.0, 3.0]}
    store, config = campaign(tmp_path, values, overrides={"b": {"carry": "none"}, "c": {"hide_scores": True}})
    _, adapter = run(store, config)
    third = {spec.task: spec for spec in adapter.submitted if spec.session_id == "s003"}
    assert "## Archive of earlier sessions" in third["a"].prompt and "**s002**" in third["a"].prompt
    assert "work of s001" in third["a"].archive_md           # carry: full shows the archived work
    assert "the archive" not in third["b"].prompt and "Archive" not in third["b"].prompt
    assert third["b"].archive_md == ""
    assert "s002" not in third["b"].prompt and "earlier work on this task" in third["b"].prompt
    assert third["b"].start_artifact == b"work of s002\n"    # it still starts from the best work
    assert "3.000" not in third["c"].prompt and "score withheld" in third["c"].prompt
    assert "{start}" in third["a"].prompt                     # braces in the task statement survive


def test_the_declaration_is_fixed_for_the_life_of_a_lineage(tmp_path):
    store, config = campaign(tmp_path, {"a": [1.0, 2.0, 2.0, 2.0]})
    run(store, config)
    changed = {"metrics": {"score": {"direction": "min", "scale": "absolute"}}}
    store, config = campaign(tmp_path, {"a": [1.0, 2.0, 2.0, 2.0, 0.5, 0.5]}, declaration=changed,
                             stop={"max_sessions": 5})
    state, _ = run(store, config)
    assert len(events(store, "declaration_mismatch")) == 1
    assert state["tasks"]["a"]["best"] == 2.0                # still "higher is better"


def test_confirm_follows_the_config_while_the_rest_stays_fixed(tmp_path):
    store, config = campaign(tmp_path, {"a": [1.0, 2.0, 3.0, 4.0]}, rescore={"a": [2.0, 3.0, 4.0]},
                             evaluation={"confirm": {"samples": 1}}, stop={"max_sessions": 1})
    run(store, config)
    assert len(events(store, "confirmation_needed")) == 1

    # the operator turns confirmation off and allows one more session: s002 is a new best, not confirmed
    store, config = campaign(tmp_path, {"a": [1.0, 2.0, 3.0, 4.0]}, rescore={"a": [2.0, 3.0, 4.0]},
                             evaluation={"confirm": None}, stop={"max_sessions": 2})
    _, adapter = run(store, config)
    assert len(events(store, "confirmation_needed")) == 1 and adapter.rescored == {}
    assert nodes(store)["s002"].rank_value == 3.0
    assert not events(store, "declaration_mismatch")
    changed = events(store, "confirm_changed")
    assert len(changed) == 1 and changed[0]["data"] == {"previous": {"samples": 1, "aggregate": "median"},
                                                        "confirm": None}
    pinned = json.loads((tmp_path / "tasks" / "a" / "task.json").read_text())["declaration"]
    assert pinned["confirm"] is None

    # and back on: the next new best is confirmed again; the change is recorded once more, not on every run
    store, config = campaign(tmp_path, {"a": [1.0, 2.0, 3.0, 4.0]}, rescore={"a": [2.0, 3.0, 4.0]},
                             evaluation={"confirm": {"samples": 1, "aggregate": "mean"}}, stop={"max_sessions": 3})
    run(store, config)
    run(store, config)
    assert len(events(store, "confirmation_needed")) == 2 and len(events(store, "confirm_changed")) == 2
    assert nodes(store)["s003"].rank_value == 3.0       # mean of 4.0 and its repeat scoring 2.0

    # the gate is still fixed
    store, config = campaign(tmp_path, {"a": [1.0, 2.0, 3.0, 4.0]}, stop={"max_sessions": 3},
                             evaluation={"confirm": {"samples": 1, "aggregate": "mean"}, "gate": ["ok", "tidy"]})
    run(store, config)
    assert len(events(store, "declaration_mismatch")) == 1


def test_a_declaration_that_ranks_by_an_unknown_metric_stops_the_task_before_any_job(tmp_path):
    store, config = campaign(tmp_path, {"a": [1.0, 2.0]}, evaluation={"rank_by": "speed"})
    state, _ = run(store, config)
    assert "rank_by must name one of the declared metrics" in state["paused"]["a"]
    assert not (tmp_path / "tasks" / "a" / "sessions" / "s000" / "job.json").exists()


@pytest.mark.parametrize("strategy", ["explore", "baseline"])
def test_other_start_strategies(tmp_path, strategy):
    store, config = campaign(tmp_path, {"a": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]}, strategy={"start": strategy, "seed": 3})
    run(store, config)
    parents = [node.parent_id for node in nodes(store).values() if not node.is_root]
    assert len(parents) == 6
    if strategy == "baseline":
        assert set(parents) == {"s000"}
    else:
        draws = [event["data"]["draw"] for event in events(store, "selection")]
        assert all(0 <= draw < 1 for draw in draws[1:])


def test_copies_of_one_piece_of_work_share_what_was_measured(tmp_path):
    values = {"a": [1.0, {"value": 3.0, "work": b"same"}, {"value": 3.9, "work": b"same"}, 2.0]}
    store, config = campaign(tmp_path, values, stop={"max_sessions": 3}, rescore={"a": [3.0, 3.2]},
                             evaluation={"confirm": {"samples": 2, "aggregate": "median"}})
    state, adapter = run(store, config)
    archived = nodes(store)
    # s001 was a new best and scored twice more: 3.0, 3.0, 3.2. s002 is the same work, measured once more at 3.9.
    assert "duplicate" in archived["s002"].flags and adapter.rescored == {"a": 2}
    assert archived["s001"].rank_value == archived["s002"].rank_value == 3.1     # the median of all four
    assert archived["s002"].chosen()["metrics"]["score"] == 3.9                    # what it measured is kept
    assert archived["s002"].chosen()["extra"]["pooled"]["nodes"] == ["s001", "s002"]
    assert state["tasks"]["a"]["best"] == 3.1 and state["tasks"]["a"]["best_session"] == "s001"


def test_a_measurement_the_adapter_does_not_trust_is_taken_again(tmp_path, monkeypatch):
    original = fake_adapter.FakeAdapter._result

    def distrusting(self, entry, default_work):
        result = original(self, entry, default_work)
        result.remeasure = entry.get("remeasure") if isinstance(entry, dict) else None
        return result

    monkeypatch.setattr(fake_adapter.FakeAdapter, "_result", distrusting)
    values = {"a": [5.0, {"value": 0.2, "remeasure": "the machine was busy"}, 4.0]}     # 0.2 is nowhere near a best
    store, config = campaign(tmp_path, values, stop={"max_sessions": 2}, rescore={"a": [3.0, 3.4]})
    _, adapter = run(store, config)
    node = nodes(store)["s001"]
    assert adapter.rescored == {"a": 2} and node.rank_value == 3.0                      # the median of 0.2, 3.0, 3.4
    assert "the machine was busy" in node.chosen()["extra"]["confirmation"]["reason"]


def test_without_a_valid_baseline_the_first_valid_session_is_the_reference(tmp_path):
    from infinievolve.core.views import matrix_rows, summary_rows
    stub = {"value": None, "checks": {"ok": False}}                       # the task starts from nothing
    values = {"a": [stub, stub, 2.0, 3.0, {"value": 4.0, "flags": {"cheated": "read the answers"}}, 3.0]}
    store, config = campaign(tmp_path, values, stop={"max_sessions": 5, "window_k": 9})
    _, adapter = run(store, config)
    rows = {row["session"]: row for row in matrix_rows(store.task("a"))}
    assert {row["reference"] for row in rows.values()} == {"s002"}
    assert rows["s003"]["vs_reference_text"] == "1.50x" and rows["s001"]["vs_reference_text"] == "-"
    assert summary_rows(store, ["a"])[0]["reference_text"] == "s002 2.000"
    by_session = {spec.session_id: spec for spec in adapter.submitted}
    inputs = tmp_path / "tasks" / "a" / "sessions"
    assert not list((inputs / "s002" / "input").glob("reference.*"))          # nothing ranked yet
    assert (inputs / "s004" / "input" / "reference.txt").read_bytes() == b"work of s002\n"
    assert "with **s002**, the first valid one, as the reference." in by_session["s004"].prompt
    # a reference that turns out not to be trustworthy hands over to the next valid session
    from infinievolve.cli import main
    (tmp_path / "config.yaml").write_text("campaign: {adapter: fake, tasks: all}\noptions: {values: {a: []}}\n")
    assert main(["flag", str(tmp_path), "--task", "a", "--session", "s002", "--flag", "wrong", "--reason", "x"]) == 0
    assert {row["reference"] for row in matrix_rows(store.task("a"))} == {"s003"}


def test_a_session_the_adapter_refused_is_prepared_again_from_the_current_state(tmp_path, monkeypatch):
    store, config = campaign(tmp_path, {"a": [1.0, 2.0, 3.0]}, stop={"max_sessions": 1})
    calls = {"n": 0}
    submit = fake_adapter.FakeAdapter.submit_session

    def gated(self, spec, session_dir, events):
        calls["n"] += 1
        if calls["n"] == 1:   # an adapter-side gate (a round pause, a quota) refuses once
            raise RuntimeError("round 1 is over; release and run again")
        return submit(self, spec, session_dir, events)
    monkeypatch.setattr(fake_adapter.FakeAdapter, "submit_session", gated)

    state, _ = run(store, config)
    assert "a" in state["paused"] and "s001" not in nodes(store)
    inputs = tmp_path / "tasks" / "a" / "sessions" / "s001" / "input"
    assert "Do a." in (inputs / "prompt.md").read_text()
    (inputs / "reference.txt").write_text("left from an earlier preparation")

    # the operator rewrites the task statement, then runs again
    monkeypatch.setattr(fake_adapter.FakeAdapter, "instruction", lambda self, task: "NEW INSTRUCTION")
    _, adapter = run(store, config)
    assert "s001" in nodes(store)                       # the same session, not a new index
    [spec] = adapter.submitted
    assert spec.session_id == "s001" and "NEW INSTRUCTION" in spec.prompt and "Do a." not in spec.prompt
    assert "NEW INSTRUCTION" in (inputs / "prompt.md").read_text()
    assert not (inputs / "reference.txt").exists()      # nothing of the old preparation is handed on
    [event] = events(store, "session_reprepared")
    assert event["session_id"] == "s001" and event["data"]["prompt_changed"] is True
