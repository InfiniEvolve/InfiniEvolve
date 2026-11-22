"""Evaluator integrity checks that need neither GPUs nor model calls."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("depthdash_evaluate", Path(__file__).resolve().parents[1] /
                                            "examples/depthdash/evaluate.py")
ev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ev)

RESULT = ("result track:deep residual:submission/residual.py model:model.py trial:0 seed:0 "
          "val_loss:4.25000 target:None reached:n/a train_time:601.200s steps:120 budget:600s "
          "record_run:False peak_mem:42.1GiB")


def test_final_result_ignores_intermediate_validation():
    metrics = ev.parse_result("step:10 val_loss:5.2\n" + RESULT, "deep", 0, 600)
    assert metrics["val_loss"] == 4.25
    assert metrics["steps"] == 120


@pytest.mark.parametrize("bad", [RESULT.replace("4.25000", "nan"), RESULT.replace("4.25000", "inf"),
    RESULT.replace("seed:0", "seed:1"), RESULT.replace("601.200s", "599.0s"),
    RESULT.replace("record_run:False", "record_run:True"), RESULT.replace("budget:600s", "budget:120s"),
    RESULT.replace("steps:120", "steps:0"), RESULT + "\n" + RESULT, "no result"])
def test_rejects_invalid_training_results(bad):
    with pytest.raises(ValueError):
        ev.parse_result(bad, "deep", 0, 600)


def test_frozen_sources_and_candidate_scope(tmp_path):
    (tmp_path / "train_gpt.py").write_text("original")
    (tmp_path / "submission").mkdir()
    candidate = tmp_path / "submission/residual.py"
    candidate.write_text("candidate")
    manifest = {"train_gpt.py": ev.digest(tmp_path / "train_gpt.py")}
    ev.verify_workspace(tmp_path, manifest)
    candidate.write_text("improved candidate")
    ev.verify_workspace(tmp_path, manifest)
    (tmp_path / "train_gpt.py").write_text("modified evaluator")
    with pytest.raises(ValueError, match="frozen file changed"):
        ev.verify_workspace(tmp_path, manifest)


def test_allows_kernel_helpers_fast_model_and_compile_scripts(tmp_path):
    (tmp_path / "submission").mkdir()
    (tmp_path / "submission/residual.py").write_text("candidate")
    for name in ("submission/kernel.cu", "submission/kernel.cuh", "submission/kernel.cpp",
                 "submission/helper.py", "submission/fast_model.py", "test_kernel.py", "build.sh"):
        (tmp_path / name).write_text("source")
    ev.verify_workspace(tmp_path, {})


def test_candidate_controls_supported_training_and_execution_knobs(tmp_path):
    import json
    (tmp_path / "submission").mkdir()
    for name in ("residual.py", "fast_model.py"):
        (tmp_path / "submission" / name).write_text("source")
    options = {"residual": "submission/residual.py", "model": "submission/fast_model.py",
               "steps": 2800, "lr": 0.03, "microbatch": 16, "checkpoint": True, "compile": "none"}
    (tmp_path / "submission/run.json").write_text(json.dumps(options))
    config = {"track": "deep", "gpus": 8, "seeds": [0, 1], "score": "time-to-target", "target": 3.28}
    assert hasattr(ev, "build_plan"), "evaluator needs a candidate-configured command plan"
    plan = ev.build_plan(tmp_path, config)
    command = plan["training"][0]
    for flag, value in (("--steps", "2800"), ("--lr", "0.03"), ("--microbatch", "16"),
                        ("--compile", "none"), ("--model", "submission/fast_model.py")):
        assert command[command.index(flag) + 1] == value
    assert "--checkpoint" in command
    assert "--nproc_per_node=8" in command
    assert plan["training"][1][-1] == "1"  # the second controller-selected seed
    assert plan["equivalence"][1] == "check_equivalence.py"
    assert "--synthetic-data" not in plan["equivalence"]
    assert "--draws" not in plan["equivalence"]  # preserve upstream test defaults


def test_step_run_checks_actual_steps_not_just_configured_limit():
    row = RESULT.replace("budget:600s record_run:False", "steps:120 record_run:True")
    assert ev.parse_result(row, "deep", 0, None, steps=120)["steps"] == 120
    with pytest.raises(ValueError):
        ev.parse_result(row.replace("steps:120", "steps:119", 1), "deep", 0, None, steps=120)


@pytest.fixture
def evaluation_task(tmp_path):
    import json
    task, workspace = tmp_path / "task", tmp_path / "workspace"
    task.mkdir()
    (workspace / "submission").mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    for name in ("fineweb_train_000000.bin", "fineweb_val_000000.bin"):
        (data / name).touch()
    (workspace / "train_gpt.py").write_text("frozen trainer")
    (workspace / "submission/residual.py").write_text("reference")
    (workspace / "submission/fast_model.py").write_text("fast model")
    (workspace / "submission/run.json").write_text(json.dumps({
        "residual": "submission/residual.py", "model": "submission/fast_model.py"}))
    (task / "frozen.json").write_text(json.dumps({"train_gpt.py": ev.digest(workspace / "train_gpt.py")}))
    (task / "evaluation.json").write_text(json.dumps({"track": "deep", "gpus": 1, "seeds": [0],
        "score": "diagnostic-loss", "budget_seconds": 600, "data_dir": str(data), "timeout_seconds": 3000}))
    return task, workspace, tmp_path / "output"


def test_equivalence_failure_prevents_training(evaluation_task, monkeypatch):
    task, workspace, output = evaluation_task
    def subprocess_boundary(command, cwd, log, timeout):
        assert command[1] == "check_equivalence.py", "training must not run after failed equivalence"
        log.write_text("equivalence track:deep residual:submission/residual.py model:submission/fast_model.py: FAIL\n")
        raise ValueError("process exited 1")
    monkeypatch.setattr(ev, "run_logged", subprocess_boundary)
    result = ev.evaluate(task, workspace, output)
    assert result["checks"]["equivalence"] is False
    assert result["checks"]["completed"] is False
    assert "val_loss" not in result["metrics"]
    assert (output / "equivalence.log").exists()


def test_fast_model_pass_and_sources_are_checked_after_training(evaluation_task, monkeypatch):
    task, workspace, output = evaluation_task
    def subprocess_boundary(command, cwd, log, timeout):
        if command[1] == "check_equivalence.py":
            log.write_text("equivalence track:deep residual:submission/residual.py model:submission/fast_model.py: PASS\n")
        else:
            assert (output / "equivalence.log").exists()
            assert command[command.index("--model") + 1] == "submission/fast_model.py"
            log.write_text(RESULT.replace("model:model.py", "model:submission/fast_model.py"))
            (workspace / "train_gpt.py").write_text("tampered trainer")
    monkeypatch.setattr(ev, "run_logged", subprocess_boundary)
    result = ev.evaluate(task, workspace, output)
    assert result["checks"]["equivalence"] is True
    assert result["checks"]["frozen_sources"] is False
    assert any("frozen file changed" in reason for reason in result["reasons"])


def test_multiple_seeds_use_upstream_checker_then_archive_each_result(evaluation_task, monkeypatch):
    import json
    task, workspace, output = evaluation_task
    config = json.loads((task / "evaluation.json").read_text())
    config["seeds"] = [0, 1]
    (task / "evaluation.json").write_text(json.dumps(config))

    def subprocess_boundary(command, cwd, log, timeout):
        if command[1] == "check_equivalence.py":
            log.write_text("equivalence track:deep residual:submission/residual.py model:submission/fast_model.py: PASS\n")
        else:
            assert (output / "equivalence.log").is_file()
            seed = command[command.index("--seed") + 1]
            log.write_text(RESULT.replace("seed:0", "seed:" + seed).replace(
                "4.25000", "4.35000" if seed == "1" else "4.25000"))
    monkeypatch.setattr(ev, "run_logged", subprocess_boundary)
    result = ev.evaluate(task, workspace, output)
    assert all(result["checks"].values())
    assert result["metrics"]["val_loss"] == pytest.approx(4.3)
    assert [row["seed"] for row in json.loads((output / "seeds.json").read_text())] == [0, 1]
    assert (output / "train-seed-0.log").is_file() and (output / "train-seed-1.log").is_file()
    assert not (workspace / "data/fineweb10B").is_symlink()


@pytest.mark.parametrize("output_text", ["", "equivalence: FAIL\n"])
def test_zero_exit_without_equivalence_pass_does_not_score(evaluation_task, monkeypatch, output_text):
    task, workspace, output = evaluation_task
    def subprocess_boundary(command, cwd, log, timeout):
        assert command[1] == "check_equivalence.py", "must not train without an equivalence PASS"
        log.write_text(output_text)
    monkeypatch.setattr(ev, "run_logged", subprocess_boundary)
    result = ev.evaluate(task, workspace, output)
    assert result["checks"]["equivalence"] is False
    assert result["checks"]["completed"] is False


def test_reference_only_runs_without_cpu_contract_or_fast_model_check(evaluation_task, monkeypatch):
    import json
    task, workspace, output = evaluation_task
    (workspace / "submission/run.json").write_text(json.dumps({"residual": "submission/residual.py"}))
    def subprocess_boundary(command, cwd, log, timeout):
        assert "train_gpt.py" in command
        assert "--model" not in command
        log.write_text(RESULT)
    monkeypatch.setattr(ev, "run_logged", subprocess_boundary)
    result = ev.evaluate(task, workspace, output)
    assert all(result["checks"].values())
    assert result["metrics"]["val_loss"] == 4.25


def test_target_requires_statistical_evidence_not_only_a_fast_run():
    pytest.importorskip("scipy.stats")
    def rows(losses):
        return [dict(val_loss=loss, train_seconds=10, steps=120, peak_memory_gib=8) for loss in losses]
    config = {"score": "time-to-target", "target": 3.28}
    metrics, reached = ev.summarize(rows([3.0, 3.01, 3.02]), config)
    assert reached and metrics["target_pvalue"] < 0.01
    assert metrics["train_seconds"] == 10
    assert not ev.summarize(rows([3.2, 3.4, 3.6]), config)[1]
    assert ev.summarize(rows([3.0, 3.0, 3.0]), config)[1]
    assert not ev.summarize(rows([3.28, 3.28, 3.28]), config)[1]


@pytest.mark.parametrize("options", [{"seed": 42}, {"steps": 500}, {"microbatch": 3},
                                     {"residual": "../external.py"}])
def test_invalid_submission_options_cannot_change_campaign_protocol(evaluation_task, options):
    import json
    task, workspace, _ = evaluation_task
    (workspace / "submission/run.json").write_text(json.dumps(options))
    with pytest.raises(ValueError):
        ev.build_plan(workspace, json.loads((task / "evaluation.json").read_text()))


def test_baseline_target_miss_can_evolve_but_infrastructure_failure_stops():
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("depthdash_adapter", Path(__file__).resolve().parents[1] /
                                                 "examples/depthdash/adapter.py")
    adapter_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter_module)
    adapter = object.__new__(adapter_module.DepthDashAdapter)
    checks = {"frozen_sources": True, "equivalence": True, "completed": True, "target": False}
    root = SimpleNamespace(valid=False, scores=[{"checks": checks, "reasons": ["target missed"]}])
    assert adapter.baseline_problem("deep", root) is None
    checks["equivalence"] = False
    assert adapter.baseline_problem("deep", root) is not None
