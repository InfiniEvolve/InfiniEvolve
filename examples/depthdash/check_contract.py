"""Small CPU contract checks, not a proof of compliance with DepthDash's rules."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path


def check(workspace):
    import torch

    sys.path.insert(0, str(workspace))
    spec = importlib.util.spec_from_file_location("candidate", workspace / "submission/residual.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    torch.manual_seed(1729)
    residual = module.Residual(512, 56).float()
    calls = []

    def layer(index):
        def apply(x):
            calls.append(index)
            return torch.tanh(x) * 0.03
        return apply

    sublayers = [layer(i) for i in range(56)]
    x = torch.randn(2, 3, 512, requires_grad=True)
    y = residual(x, sublayers)
    assert calls == list(range(56)), "call every black-box sublayer exactly once, in order"
    assert y.shape == x.shape and torch.isfinite(y).all(), "invalid output shape or values"
    y.square().mean().backward()
    assert x.grad is not None and torch.isfinite(x.grad).all(), "invalid input gradients"
    for name, p in residual.named_parameters():
        assert p.grad is not None and torch.isfinite(p.grad).all(), f"invalid gradients: {name}"
    with torch.no_grad():
        original = residual(x.detach(), sublayers)
        changed = x.detach().clone()
        changed[0, 1] += 2.0
        perturbed = residual(changed, sublayers)
        mask = torch.ones(2, 3, dtype=torch.bool)
        mask[0, 1] = False
        assert torch.allclose(original[mask], perturbed[mask], atol=1e-6, rtol=1e-5), "mixes tokens/batches"
    return {"contract_smoke": True, "residual_parameters": sum(p.numel() for p in residual.parameters())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.workspace.resolve())))
