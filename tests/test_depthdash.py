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


def test_rejects_extra_files_and_symlinks(tmp_path):
    (tmp_path / "submission").mkdir()
    (tmp_path / "submission/residual.py").write_text("candidate")
    extra = tmp_path / "extra.py"
    extra.write_text("untracked injection")
    with pytest.raises(ValueError, match="only submission"):
        ev.verify_workspace(tmp_path, {})
    extra.unlink()
    (tmp_path / "data").symlink_to(tmp_path / "submission")
    with pytest.raises(ValueError, match="symlink"):
        ev.verify_workspace(tmp_path, {})
