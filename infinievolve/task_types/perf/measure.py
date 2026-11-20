"""Paired timing: a candidate and a reference timed in alternation on the same machine.

Two timings taken at different times or on different machines differ by more than most improvements are
worth. Timing the candidate and a reference back to back, in an order balanced over the blocks (AB, BA,
BA, AB, ...), cancels what the machine and the moment add, and leaves the ratio between the two.

    blocks = run_blocks(time_candidate, time_reference, blocks=4, budget_seconds=240)
    result = paired(blocks)     # {"ratio": reference time / candidate time, "dispersion": ..., "blocks": n}

`ratio` above 1 means the candidate is faster than the reference. `dispersion` is the relative spread of
the per-block ratios: 0.05 means they typically differ by about 5%.

Standard library only, and no import from the rest of the package: an adapter sends this file along with
the script its jobs run, so that the scheduler and the job compute the same thing.
"""
from __future__ import annotations

import math
import statistics
import time


def orders(blocks: int) -> list:
    """The order of the two arms in each block, balanced: AB, BA, BA, AB, AB, BA, ..."""
    pattern = ("AB", "BA", "BA", "AB")
    return [pattern[index % 4] for index in range(blocks)]


def run_blocks(time_candidate, time_reference, *, blocks: int = 4, budget_seconds: float = 240,
               clock=time.time) -> list:
    """Time both arms `blocks` times in a balanced order; each callable returns one timing (any unit, the same
    for both). Stops early, between blocks, once the budget is spent. Returns [{candidate, reference}]."""
    arms = {"A": time_reference, "B": time_candidate}
    deadline, measured = clock() + budget_seconds, []
    for order in orders(blocks):
        if measured and clock() >= deadline:
            break
        times = {arm: float(arms[arm]()) for arm in order}
        measured.append({"reference": times["A"], "candidate": times["B"]})
    return measured


def paired(blocks: list) -> dict:
    """The candidate's speed against the reference's from paired blocks: the median of the per-block ratios
    (taken on a log scale, where a ratio and its inverse weigh the same) and their relative spread."""
    logs = [math.log(block["reference"] / block["candidate"]) for block in blocks
            if block.get("reference", 0) > 0 and block.get("candidate", 0) > 0]
    if not logs:
        return {"ratio": None, "dispersion": None, "blocks": 0}
    middle = statistics.median(logs)
    spread = statistics.median(abs(value - middle) for value in logs)   # median absolute deviation
    return {"ratio": math.exp(middle), "dispersion": math.exp(1.4826 * spread) - 1, "blocks": len(logs)}


def pooled(values, how: str = "median"):
    """One value for several measurements of the same work; None for none."""
    values = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    if not values:
        return None
    return statistics.median(values) if how == "median" else statistics.fmean(values)


# -- timing the right thing ---------------------------------------------------------------------
# Run a script with time.process_time reading the wall clock:  python3 -c WALLCLOCK <script> [arguments]
# Some workloads time themselves with process_time, which counts CPU seconds of the calling thread's
# process: other threads' work is missed in short windows and counted several times over in long ones.
# What a speedup is meant to say is how long one waits.
WALLCLOCK = ("import runpy, sys, time; time.process_time = time.perf_counter; "
             "time.process_time_ns = time.perf_counter_ns; sys.argv = sys.argv[1:]; "
             "runpy.run_path(sys.argv[0], run_name='__main__')")


def digest(root, paths=None) -> dict:
    """sha256 of files under root: the given relative paths, or every file. Taken before and after a timing,
    it shows whether what was timed stayed what it was."""
    import hashlib
    from pathlib import Path
    root = Path(root)
    if paths is None:
        paths = sorted(str(path.relative_to(root)) for path in root.rglob("*")
                       if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts)
    return {str(path): hashlib.sha256((root / path).read_bytes()).hexdigest() if (root / path).is_file() else None
            for path in paths}


def snapshot(tree, target):
    """A private copy of a tree to time, so that nothing still running can change what is being timed."""
    import shutil
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(tree, target, symlinks=True)
    return target


# -- a quiet machine (off unless asked for) -----------------------------------------------------
JOB_TAG = "INFINIEVOLVE_JOB_TAG"


def tag_job(environment: dict | None = None) -> str:
    """Mark this process, and through inheritance everything it starts, as one job's. Returns the tag."""
    import os
    tag = os.environ.get(JOB_TAG) or os.urandom(8).hex()
    os.environ[JOB_TAG] = tag
    if environment is not None:
        environment[JOB_TAG] = tag
    return tag


def reap_tagged(tag: str) -> list:
    """End every process that carries this job's tag, except this process and its ancestors: what an agent or
    a candidate left running would share the machine with the next timing. Only processes of the same user
    that inherited the tag can match, so nothing outside the job is touched. Returns what was ended.

    This kills processes. Nothing calls it unless the adapter is told to; see the adapter's own notes."""
    import os
    import signal
    keep, pid = set(), os.getpid()
    while pid > 1 and pid not in keep:
        keep.add(pid)
        try:
            pid = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    ended = []
    for entry in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not entry.isdigit() or int(entry) in keep:
            continue
        try:
            environment = open(f"/proc/{entry}/environ", "rb").read().split(b"\0")
            if f"{JOB_TAG}={tag}".encode() not in environment:
                continue
            command = open(f"/proc/{entry}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")[:200]
            os.kill(int(entry), signal.SIGKILL)
            ended.append({"pid": int(entry), "command": command})
        except (OSError, ValueError):
            continue   # gone already, or not ours to read
    return ended
