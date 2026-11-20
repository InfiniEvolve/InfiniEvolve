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
    assert declaration.rank_by == "size" and declaration.show == ("size",) and declaration.gate == ()
    assert parse_declaration(declaration.to_dict()) == declaration


@pytest.mark.parametrize("data, message", [
    ({"metrics": {}}, "at least one metric"),
    ({"metrics": {"a": {}, "b": {}}}, "rank_by must name"),
    ({"metrics": {"a": {"direction": "up"}}}, "direction"),
    ({"metrics": {"a": {"scale": "log"}}}, "scale"),
    ({"metrics": {"a": {"scale": "ratio", "anchor": 0}}}, "anchor"),
    ({"metrics": {"a": {"format": "{:d}"}}}, "format"),
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


# -- the example, end to end ----------------------------------------------------------------
@pytest.mark.skipif(shutil.which("git") is None, reason="the local example adapter needs git")
def test_the_local_example_runs(tmp_path, capsys):
    example = tmp_path / "local"
    shutil.copytree(REPOSITORY / "examples" / "local", example,
                    ignore=shutil.ignore_patterns("__pycache__"))
    root = example / "campaigns" / "demo"
    for leftover in ("tasks", "events.jsonl", "state.json", "summary.json", "summary.md", "provenance.json", "exports"):
        path = root / leftover
        shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
    assert main(["run", str(root)]) == 0
    assert main(["export", str(root)]) == 0
    assert main(["flag", str(root), "--task", "golf", "--session", "s005", "--flag", "checked_by_hand",
                 "--reason", "looked wrong", "--level", "hard"]) == 0
    assert main(["report", str(root), "--task", "golf"]) == 0
    printed = capsys.readouterr().out
    assert "| golf | bytes |" in printed and "checked_by_hand (hard)" in printed
    task = root / "tasks" / "golf"
    final = (task / "archive" / "artifacts" / "s005.patch").read_bytes()
    work = tmp_path / "check"
    shutil.copytree(example / "tasks" / "golf" / "workspace", work)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "apply", "-"], cwd=work, input=final, check=True)
    assert (work / "solution.py").stat().st_size == 163          # the archived work reproduces the score
    assert (root / "exports" / "golf" / "lineage.mmd").is_file()
    assert "s004" in (root / "exports" / "best_so_far.csv").read_text()
