# DepthDash pilot

This task uses InfiniEvolve's local adapter **inside an allocated GPU job**. It freezes
[DepthDash at 96d683c](https://github.com/keyu-wang-2002/DepthDash/tree/96d683cf3284522e22696d9e2de135ff0859c2fd)
and lets the coding agent edit only `submission/residual.py`.

The initial experiment is deliberately small: `deep` (28 layers, width 512), one H100,
600 seconds of timed training per candidate, seed 0, Muon lr 0.025, microbatch 8,
sublayer compilation, and the original 20 warmup steps and 10,485,760-token validation.
It starts from Pre-LN and runs three agent sessions, each inheriting the best valid patch
and the full archive. The baseline is additional to those three sessions.

**This measures final validation loss under a fixed training-time budget. It is not an
official DepthDash record.** The pinned upstream has no calibrated target for `deep`
or `extreme_deep`, and its record hardware is 8 H100 SXM GPUs. A single-seed pilot does
not establish a reliable architecture improvement. Compare the best candidate with
Pre-LN on additional fixed seeds before making that claim.

## Prepare

Use a clean checkout at the pinned commit and download FineWeb once. The pilot uses
the first four training shards and the validation shard from
`kjj0/fineweb10B-gpt2`, revision `889765ea1f903759787add96995d81171b632d0c`.
Record and verify the shard checksums before running. No data download occurs in the evaluator.

```sh
python examples/depthdash/prepare.py \
  --upstream /path/to/DepthDash \
  --task /path/to/deep \
  --data-dir /path/to/fineweb-shards
```

Copy [`config.example.yaml`](config.example.yaml) into a new campaign directory.
Replace `YOUR_MODEL` with the model exposed by your existing Claude Code installation
and set `options.task_dir` to the generated `deep` directory. The folder name must
match `campaign.tasks`.

```sh
mkdir -p campaigns/depthdash
cp examples/depthdash/config.example.yaml campaigns/depthdash/config.yaml
# Edit the model and task path, then preview without starting an agent or training:
python -m infinievolve.cli check campaigns/depthdash --prompt
# Inside a one-GPU allocation:
python -m infinievolve.cli run campaigns/depthdash
python -m infinievolve.cli report campaigns/depthdash --task deep
python -m infinievolve.cli export campaigns/depthdash
```

The template starts from the best valid patch and includes the full archive. It uses
Claude file tools only and keeps permission checks enabled. Authenticate Claude through
your existing login/environment; keep credentials out of the campaign file.

Run the scheduler on the GPU worker, not the cluster login node. Reuse an existing
CUDA-enabled Torch 2.11 environment only after a GPU smoke test. The current MPI-IS
pilot uses its existing `2.11.0+cu129` environment without modifying it.

## Checks and evidence

- Every upstream tracked file is checked against its SHA-256 before and after training.
- Only `submission/residual.py` can differ from the baseline. Extra files and candidate
  symlinks are rejected.
- A small CPU check exercises forward/backward, call order, output shape, finite
  gradients, and independence across tokens. This is a smoke check, not a proof of
  every possible residual's compliance. Final candidates still need source review.
- Failed commands, wrong seeds, missing/duplicate results, nonfinite loss, and incomplete
  training cannot receive valid scores. A broken baseline stops the campaign.
- Each evaluation writes the full training output, upstream source-containing log,
  invocation, candidate hash, and score into that session's `raw/depthdash/` directory,
  so the local adapter archives them before deleting its scratch copy.
- The pilot gives Claude file tools only, with permission to edit the submission. It
  cannot launch training during its session. The evaluator runs once afterward.

The generated task caps each Claude session at 8 minutes; the campaign template caps
each session at $3 of reported model API usage. Configure your scheduler's wall-time
limit separately: the MPI-IS pilot uses 2 hours for the complete GPU allocation and
10 minutes for a separate GPU smoke job. These allocation limits are not implemented
by this example's local adapter. Allocation time includes agent time, compilation,
warmup and validation; `--budget 600` covers timed training only.

Run `python -m pytest -q tests/test_depthdash.py` for evaluator integrity tests that
need neither a GPU nor model calls. The CPU residual check needs PyTorch:
`python examples/depthdash/check_contract.py /path/to/deep/workspace`.
