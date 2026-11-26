"""The declaration, selection, the stop rule, artifact kinds, and the neutrality of the framework's words."""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest

import infinievolve
from infinievolve.artifacts import artifact_kind
from infinievolve.cli import main
from infinievolve.core import selection, stopping
from infinievolve.core.prompt import extract_report, report_block
from infinievolve.core.schema import parse_declaration
from infinievolve.core.types import Node

PACKAGE = Path(infinievolve.__file__).parent
REPOSITORY = PACKAGE.parent


def node(index, value, *, valid=True, parent=None, flags=None):
    return Node(session_id=f"s{index:03d}", index=index, parent_id=parent, start_strategy="best", task="t",
                agent=None, chosen_label="final", artifact={}, valid=valid, rank_metric="m", rank_value=value,
                scores=[], flags=flags or {})


def metric(**spec):
    return parse_declaration({"metrics": {"m": spec}}).rank


# -- declaration -----------------------------------------------------------------------------
def test_a_declaration_with_one_metric_ranks_by_it():
    declaration = parse_declaration({"metrics": {"size": {"direction": "min"}}})
    assert declaration.rank_by == "size" and declaration.show == ("size",) and declaration.gate is None
    assert parse_declaration(declaration.to_dict()) == declaration


@pytest.mark.parametrize("data, message", [
    ({"metrics": {}}, "at least one metric"),
    ({"metrics": {"a": {}, "b": {}}}, "rank_by must name"),
    ({"metrics": {"a": {"direction": "up"}}}, "direction"),
    ({"metrics": {"a": {"scale": "log"}}}, "scale"),
    ({"metrics": {"a": {"scale": "ratio", "anchor": 0}}}, "anchor"),
    ({"metrics": {"a": {"format": "{:d}"}}}, "format"),
    ({"metrics": {"a": {"marks": [1.0]}}}, "marks"),
    ({"metrics": {"a": {"scale": "ratio", "marks": {"zero": 0}}}}, "marks.zero"),
    ({"metrics": {"a": {}}, "show": ["b"]}, "undeclared"),
    ({"metrics": {"a": {}}, "flags": {"x": "severe"}}, "levels"),
    ({"metrics": {"a": {}}, "confirm": {"samples": 0}}, "confirm"),
    ({"metrics": {"a": {}}, "extra": 1}, "unsupported"),
])
def test_a_wrong_declaration_is_refused(data, message):
    with pytest.raises(ValueError, match=message):
        parse_declaration(data)


def test_only_some_keys_can_be_overridden_by_a_campaign():
    declaration = parse_declaration({"metrics": {"a": {}, "b": {}}, "rank_by": "a"})
    assert declaration.merged({"rank_by": "b", "show": ["a", "b"]}).rank_by == "b"
    with pytest.raises(ValueError, match="unsupported"):
        declaration.merged({"metrics": {}})


def test_what_counts_as_an_improvement_depends_on_direction_and_scale():
    assert metric(direction="max", scale="ratio").improved(1.06, 1.0, 5)
    assert not metric(direction="max", scale="ratio").improved(1.05, 1.0, 5)
    assert metric(direction="min", scale="ratio").improved(0.94, 1.0, 5)
    assert metric(direction="max", scale="absolute").improved(0.56, 0.5, 0.05)
    assert not metric(direction="min", scale="absolute").improved(98, 100, 2)
    assert metric(direction="min", scale="absolute").improved(97, 100, 2)
    assert not metric(scale="ratio").usable(0) and metric(scale="absolute").usable(-3)
    assert not metric().usable(True) and not metric().usable(float("nan"))


# -- selection and stopping -------------------------------------------------------------------
def test_best_picks_by_direction_and_skips_what_is_not_ranked():
    pool = [node(0, 5.0), node(1, 9.0, valid=False), node(2, 3.0), node(3, 1.0, flags={"x": {"level": "hard"}}),
            node(4, 3.0), node(5, 2.0, flags={"y": {"level": "soft"}})]
    assert selection.select("best", pool, metric(direction="max"), seed=0, index=6).parent_id == "s000"
    assert selection.select("best", pool, metric(direction="min"), seed=0, index=6).parent_id == "s005"
    assert selection.best_node(pool[:5], metric(direction="min")).session_id == "s002"   # the earlier of equals


def test_explore_prefers_better_and_less_explored_nodes_on_either_scale():
    pool = [node(0, 1.0), node(1, 2.0, parent="s000"), node(2, 4.0, parent="s001")]
    for spec in ({"scale": "ratio"}, {"scale": "absolute"}):
        weights = selection.explore_weights(pool, selection.children(pool), metric(**spec), 2.0)
        assert weights["s002"] > weights["s001"] > weights["s000"]
    lower = selection.explore_weights(pool, {}, metric(direction="min"), 2.0)
    assert lower["s000"] > lower["s001"] > lower["s002"]
    first = selection.select("explore", pool, metric(), seed=7, index=3)
    assert first == selection.select("explore", pool, metric(), seed=7, index=3)     # the same draw when resumed


def test_where_the_best_curve_starts():
    sessions = [node(1, None, valid=False), node(2, 3.0), node(3, 2.0)]
    assert stopping.best_curve([node(0, 1.0)] + sessions, metric(anchor="baseline")) == [1.0, 1.0, 3.0, 3.0]
    assert stopping.best_curve([node(0, 1.0, valid=False)] + sessions, metric(anchor="baseline")) == [None, None, 3.0, 3.0]
    assert stopping.best_curve([node(0, 1.0)] + sessions, metric(anchor="none")) == [None, None, 3.0, 3.0]
    assert stopping.best_curve(sessions, metric(anchor=2.5)) == [2.5, 2.5, 3.0, 3.0]


def test_the_stop_rule():
    rule = dict(max_sessions=5, window_k=2, min_improvement=10)
    ratio = metric(scale="ratio")
    assert not stopping.check([node(0, 1.0), node(1, 1.0)], ratio, **rule).stop                     # too few sessions
    assert stopping.check([node(0, 1.0), node(1, 1.0), node(2, 1.1)], ratio, **rule).stop           # not more than 10%
    assert not stopping.check([node(0, 1.0), node(1, 1.0), node(2, 1.2)], ratio, **rule).stop
    unanchored = metric(scale="ratio", anchor="none")
    assert not stopping.check([node(0, 1.0), node(1, None, valid=False), node(2, 1.0)], unanchored, **rule).stop
    assert stopping.check([node(index, float(index + 1)) for index in range(6)], ratio, **rule).reason == \
        "reached max_sessions=5"


# -- artifact kinds and the report ------------------------------------------------------------
def test_the_patch_kind_reads_a_diff():
    data = (b"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-old\n+new\n same\n"
            b"diff --git a/y.txt b/y.txt\n--- /dev/null\n+++ b/y.txt\n@@ -0,0 +1 @@\n+added\n")
    kind = artifact_kind("")
    assert kind.name == "patch"
    assert kind.summary(data) == {"files": ["x.py", "y.txt"], "added": 2, "removed": 1}
    assert kind.describe(kind.summary(data)) == "changes x.py, y.txt (+2/-1 lines against the baseline)"
    assert kind.fingerprint(data) == kind.fingerprint(bytes(data)) != kind.fingerprint(data + b"\n")
    assert kind.render(b"") is None and "```diff" in kind.render(data)
    with pytest.raises(ValueError, match="unknown artifact kind"):
        artifact_kind("weights")


def test_the_report_block_follows_the_declaration_and_is_read_back():
    declaration = parse_declaration({"metrics": {"a": {}}, "report_fields": [
        {"name": "summary", "hint": "two sentences"}, {"name": "open_questions"}]})
    block = report_block(declaration)
    assert block.splitlines() == ["SESSION-REPORT-BEGIN", "summary: <two sentences>", "open_questions:",
                                  "SESSION-REPORT-END"]
    said = ["early SESSION-REPORT-BEGIN\nsummary: first\nSESSION-REPORT-END", "", "then\n" + block.replace(
        "<two sentences>", "second") + "\nbye"]
    assert extract_report(said) == "summary: second\nopen_questions:"
    assert extract_report("no block here").startswith("(no report block")
    assert extract_report([]) == ""


# -- the framework's own words --------------------------------------------------------------
# Words that belong to one kind of task. The framework's own code, prompt and template must not use them:
# what a task is about comes from its adapter, its declaration and its artifact kind.
TASK_WORDS = re.compile(r"\b(speed\w*|fast\w*|slow\w*|oracle|paired|pairing|simulation|reward|benchmark\w*|"
                        r"patch\w*|diffs?|working tree|commit\w*|gpu|cluster|latency|throughput)\b", re.I)


def test_the_framework_names_no_kind_of_task():
    files = sorted((PACKAGE / "core").glob("*.py")) + sorted((PACKAGE / "prompts").glob("*")) + \
        sorted((PACKAGE / "templates").glob("*")) + [PACKAGE / "config.py", PACKAGE / "cli.py", PACKAGE / "export.py"]
    found = [f"{path.relative_to(REPOSITORY)}:{number}: {line.strip()}" for path in files
             for number, line in enumerate(path.read_text().splitlines(), start=1) if TASK_WORDS.search(line)]
    assert not found, "\n".join(found)


# -- the example and the task-directory commands, end to end -----------------------------------
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="the local adapter needs git")


@needs_git
def test_the_example_runs_from_its_task_directory(tmp_path, capsys):
    task = tmp_path / "golf"
    shutil.copytree(REPOSITORY / "examples" / "golf", task, ignore=shutil.ignore_patterns("__pycache__", "runs"))
    assert main(["run", str(task)]) == 0
    assert main(["export", str(task)]) == 0
    assert main(["flag", str(task), "--task", "golf", "--session", "s005", "--flag", "checked_by_hand",
                 "--reason", "looked wrong", "--level", "hard"]) == 0
    assert main(["report", str(task), "--task", "golf"]) == 0
    printed = capsys.readouterr().out
    assert "| golf | bytes | 8 | 8 | s000 466 |" in printed and "checked_by_hand (hard)" in printed
    assert "Reference: s000." in printed
    run = task / "runs" / "default"
    prompt = (run / "tasks" / "golf" / "sessions" / "s002" / "input" / "prompt.md").read_text()
    assert "How your work is ranked: by `bytes`, lower being better, among versions that pass every check." in prompt
    final = (run / "tasks" / "golf" / "archive" / "artifacts" / "s005.patch").read_bytes()
    work = tmp_path / "check"
    shutil.copytree(task / "workspace", work)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "apply", "-"], cwd=work, input=final, check=True)
    assert (work / "solution.py").stat().st_size == 163          # the archived work reproduces the score
    assert (run / "exports" / "golf" / "lineage.mmd").is_file()
    assert "s004" in (run / "exports" / "best_so_far.csv").read_text()


@needs_git
def test_a_new_task_directory_runs_with_three_edits(tmp_path, capsys):
    task = tmp_path / "shorter"
    assert main(["init", str(task)]) == 0
    assert main(["init", str(task)]) == 1                         # never over what is there
    (task / "workspace" / "note.txt").write_text("a rather long sentence\n")
    (task / "evaluate.py").write_text(
        "import json, pathlib\n"
        "size = pathlib.Path('note.txt').stat().st_size\n"
        "print(json.dumps({'checks': {'not_empty': size > 0}, 'metrics': {'score': -size}}))\n")
    agent = "python3 -c \"import pathlib; p = pathlib.Path('note.txt'); p.write_text(p.read_text()[:-2])\""
    assert main(["run", str(task), "--agent-command", agent, "--sessions", "3"]) == 0
    printed = capsys.readouterr().out
    assert "| shorter | score | 3 | 3 | s000 -23.000 | -17.000 | +6.000 | s003 |" in printed
    with pytest.raises(SystemExit, match="already has a config.yaml"):
        main(["run", str(task), "--sessions", "5"])
    assert main(["run", str(task), "--name", "second", "--agent-command", agent, "--sessions", "1"]) == 0
    assert (task / "runs" / "second" / "tasks" / "shorter" / "sessions" / "s001" / "node.json").is_file()


def test_named_agents_become_command_lines():
    from infinievolve.local import agent_command
    command, shell = agent_command({"spec": "claude/some-model:high"})
    assert not shell and command[:6] == ["claude", "-p", "--model", "some-model", "--effort", "high"]
    command, shell = agent_command("codex/another")
    assert not shell and command[:2] == ["codex", "exec"] and "another" in command and command[-1] == "-"
    assert agent_command({"command": "my-agent --go"}) == ("my-agent --go", True)
    for wrong in ("claude", "other/model", {}):
        with pytest.raises(ValueError):
            agent_command(wrong)


def test_without_a_declared_gate_every_reported_check_must_pass():
    open_gate = parse_declaration({"metrics": {"a": {}}})
    assert open_gate.valid({"x": True, "y": True}) and open_gate.valid({}) and not open_gate.valid({"x": True, "y": False})
    assert open_gate.failed({"x": True, "y": False}) == ["y"]
    named = parse_declaration({"metrics": {"a": {}}, "gate": ["x"]})
    assert named.valid({"x": True, "y": False}) and not named.valid({"y": True}) and named.failed({}) == ["x"]
    assert parse_declaration({"metrics": {"a": {}}, "gate": []}).valid({"x": False})
    assert parse_declaration(open_gate.to_dict()).gate is None


def test_the_ranking_sentence_comes_from_the_declaration():
    from infinievolve.core.prompt import ranking_text
    assert ranking_text(parse_declaration({"metrics": {"size": {"direction": "min"}}})) == \
        "by `size`, lower being better, among versions that pass every check."
    full = parse_declaration({"metrics": {"rate": {}}, "gate": ["builds"], "confirm": {"samples": 2}})
    assert ranking_text(full) == ("by `rate`, higher being better, among versions that pass `builds`. A new best is "
                                  "scored 2 more time(s) and ranked by the median.")
    assert ranking_text(parse_declaration({"metrics": {"rate": {}}, "gate": []})) == "by `rate`, higher being better."


# -- finding adapters and recording their events ----------------------------------------------
def test_an_adapter_file_in_a_package_can_import_its_neighbours(tmp_path):
    from infinievolve.adapters import find_factory
    package = tmp_path / "mine"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "helper.py").write_text("VALUE = 41\n")
    (package / "adapter.py").write_text("from .helper import VALUE\n\ndef make(config):\n    return VALUE + 1\n")
    (tmp_path / "single.py").write_text("def make(config):\n    return 7\n")
    assert find_factory("mine/adapter.py:make", tmp_path)(None) == 42
    assert find_factory("single.py:make", tmp_path)(None) == 7
    assert find_factory(str(package / "adapter.py") + ":make")(None) == 42      # an absolute path too
    with pytest.raises(ValueError, match="not found"):
        find_factory("missing.py:make", tmp_path)


def test_an_adapter_may_record_events_of_its_own(tmp_path):
    from infinievolve.core.events import EventLog
    log = EventLog(tmp_path / "events.jsonl").for_task("t")
    log.emit("job_held", "s001", reason="memory")
    log.emit("adapter_image_pulled", "s001", seconds=3)
    with pytest.raises(ValueError, match="adapter_"):
        log.emit("made_up", "s001")
    assert [event["type"] for event in log.read()] == ["job_held", "adapter_image_pulled"]


def test_check_says_what_a_run_would_do_and_starts_nothing(tmp_path, capsys):
    task = tmp_path / "golf"
    shutil.copytree(REPOSITORY / "examples" / "golf", task, ignore=shutil.ignore_patterns("__pycache__", "runs"))
    assert main(["check", str(task), "--prompt", "--sessions", "4"]) == 0
    printed = capsys.readouterr().out
    assert "ranked by    bytes (lower is better; absolute; the best-so-far curve starts at: baseline)" in printed
    assert "valid when   every check the evaluator reports" in printed
    assert "sessions     at most 4; stops earlier when the last 3 bring no gain above 0" in printed
    assert "You are shortening the program described below." in printed and "## Task" in printed
    assert not (task / "runs").exists()
