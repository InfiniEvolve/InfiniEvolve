"""The snapshot of a campaign that the watch page shows, and the hand-over to EvolveVista, which serves it."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import types

import pytest

from infinievolve import cli
from infinievolve.cli import main
from infinievolve.snapshot import snapshot, stamp

REPOSITORY = Path(__file__).resolve().parents[1]
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="the local adapter needs git")


@pytest.fixture(scope="module")
def golf(tmp_path_factory):
    """The bundled example, run once: nine sessions, the best at s005, three duplicates."""
    if shutil.which("git") is None:
        pytest.skip("the local adapter needs git")
    task = tmp_path_factory.mktemp("watch") / "golf"
    shutil.copytree(REPOSITORY / "examples" / "golf", task, ignore=shutil.ignore_patterns("__pycache__", "runs"))
    assert main(["run", str(task)]) == 0
    return task / "runs" / "default"


def test_the_snapshot_holds_what_the_archive_holds(golf):
    data = snapshot(golf)
    assert data["format"] == "infinievolve-campaign" and data["version"] == 1
    task = data["tasks"][0]
    assert task["metric"] == {"name": "bytes", "direction": "min", "scale": "absolute", "format": "{:.0f}",
                              "anchor": "baseline", "marks": []}
    nodes = {n["id"]: n for n in task["nodes"]}
    assert [nodes[s]["display"] for s in ("s000", "s001", "s005")] == ["466", "286", "163"]
    assert nodes["s001"]["parent"] == "s000" and nodes["s001"]["status"] == "archived"
    assert nodes["s006"]["duplicate"] and not nodes["s006"]["excluded"]
    assert nodes["s003"]["report"].startswith("approach:") and nodes["s003"]["metrics"] == {"bytes": 222}
    assert nodes["s003"]["texts"] == {"bytes": "222"}
    assert task["stopped"] and task["paused"] is None


def test_running_sessions_and_the_stamp(golf, tmp_path):
    copy = tmp_path / "run"
    shutil.copytree(golf, copy)
    before = stamp(copy)
    session = copy / "tasks" / "golf" / "sessions" / "s009"
    (session / "input").mkdir(parents=True)
    (session / "input" / "spec.json").write_text(json.dumps({"session_id": "s009", "index": 9, "parent_id": "s005"}))
    assert snapshot(copy)["tasks"][0]["nodes"][-1]["status"] == "prepared"
    (session / "job.json").write_text("{}")
    last = snapshot(copy)["tasks"][0]["nodes"][-1]
    assert (last["id"], last["status"], last["parent"]) == ("s009", "running", "s005")
    assert stamp(copy) != before


def test_watch_needs_a_run(tmp_path, capsys):
    assert main(["init", str(tmp_path / "fresh")]) == 0
    assert main(["watch", str(tmp_path / "fresh")]) == 1
    assert "no runs yet" in capsys.readouterr().err


def test_watch_says_how_to_install_evolvevista(golf, monkeypatch):
    monkeypatch.setitem(sys.modules, "evolve_vista", None)   # import fails as if it were not installed
    with pytest.raises(SystemExit, match="EvolveVista"):
        main(["watch", str(golf)])
    task = golf.parents[1]
    before = sorted(p.name for p in (task / "runs").iterdir())
    with pytest.raises(SystemExit, match="EvolveVista"):
        main(["run", str(task), "--name", "watched", "--watch"])
    assert sorted(p.name for p in (task / "runs").iterdir()) == before, "nothing is written before it says so"


def test_install_watch_installs_the_plugin_into_this_python(tmp_path, monkeypatch, capsys):
    """From its repository by default, a checkout editable; with pip when this Python has it, else with uv."""
    calls = []
    monkeypatch.setattr("subprocess.call", lambda command: calls.append(command) or 0)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "pip" else None)
    assert main(["install-watch"]) == 0
    assert calls[-1] == [sys.executable, "-m", "pip", "install", "--upgrade", cli.EVOLVE_VISTA]
    assert main(["install-watch", "--from", str(tmp_path)]) == 0
    assert calls[-1][-2:] == ["-e", str(tmp_path)]
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    assert main(["install-watch"]) == 0
    assert calls[-1] == ["uv", "pip", "install", "--python", sys.executable, "--upgrade", cli.EVOLVE_VISTA]
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="neither pip nor uv"):
        main(["install-watch"])
    assert "install-watch" in cli.WATCH_MISSING


def test_watch_serves_evolvevista_with_this_snapshot(golf, monkeypatch):
    """The options go to EvolveVista as given, with InfiniEvolve's own reader of the campaign."""
    seen = {}

    class Reader:
        def __init__(self, snapshot, stamp):
            self.snapshot, self.stamp = snapshot, stamp

    def serve(root, args, reader):
        seen.update(root=root, port=args.port, analyst=args.analyst, no_agents=args.no_agents, reader=reader)
        return "server"
    package = types.ModuleType("evolve_vista"); package.Reader = Reader
    module = types.ModuleType("evolve_vista.cli"); module.serve = serve
    monkeypatch.setitem(sys.modules, "evolve_vista", package)
    monkeypatch.setitem(sys.modules, "evolve_vista.cli", module)
    monkeypatch.setattr(cli, "hold", lambda server: seen.update(held=server))
    assert main(["watch", str(golf), "--port", "9001", "--analyst", "codex/m:high", "--no-agents"]) == 0
    assert (seen["root"], seen["port"], seen["analyst"], seen["no_agents"], seen["held"]) == (golf, 9001, ["codex/m:high"], True, "server")
    assert seen["reader"].snapshot is snapshot and seen["reader"].stamp is stamp
