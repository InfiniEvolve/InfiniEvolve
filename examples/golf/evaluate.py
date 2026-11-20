"""Scores a version of the golf task: run in a copy of the work, print one JSON line."""
import importlib.util
import json
from pathlib import Path

PRIMES = {2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97}


def main() -> dict:
    source = Path("solution.py")
    files = sorted(path.name for path in Path(".").iterdir() if path.name not in (".git", "__pycache__"))
    checks = {"only_solution": files == ["solution.py"], "tests_pass": False}
    reasons = [] if checks["only_solution"] else [f"files other than solution.py: {files}"]
    if not source.is_file():
        return {"checks": checks, "metrics": {}, "reasons": reasons + ["solution.py is missing"]}
    try:
        spec = importlib.util.spec_from_file_location("solution", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        wrong = [n for n in range(-5, 101) if bool(module.is_prime(n)) != (n in PRIMES)]
        checks["tests_pass"] = not wrong
        if wrong:
            reasons.append(f"wrong answer for {wrong[:5]}")
    except Exception as error:   # whatever the candidate does wrong is a failed check, not a crash here
        reasons.append(f"{type(error).__name__}: {error}")
    return {"checks": checks, "metrics": {"bytes": source.stat().st_size}, "reasons": reasons}


if __name__ == "__main__":
    print(json.dumps(main()))
