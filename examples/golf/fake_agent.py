"""A stand-in for a coding agent, so the example runs without one: each session it makes the next small
edit that shortens solution.py, and ends with the report block the prompt asks for. Once it has no edit
left it changes nothing, which is how the example reaches its stop rule."""
import os
from pathlib import Path
import re

source = Path("solution.py")
text = source.read_text()
lines = text.splitlines()
steps = [
    ("removed the comments", [line for line in lines if not line.strip().startswith("#")]),
    ("removed the docstrings", [line for line in lines if not line.strip().startswith('"""')]),
    ("removed the blank lines", [line for line in lines if line.strip()]),
    ("shortened the names", re.sub(r"\bdivisor\b", "d", re.sub(r"\bnumber\b", "n", text)).splitlines()),
    ("used an augmented assignment", text.replace("d = d + 1", "d += 1").splitlines()),
]
done = None
for what, changed in steps:
    if changed != lines:
        source.write_text("\n".join(changed) + "\n")
        done = what
        break
prompt = Path(os.environ["EVOLVE_PROMPT"]).read_text()
print(f"(read a prompt of {len(prompt)} characters)")
print("SESSION-REPORT-BEGIN")
print(f"approach: {done or 'found nothing left to shorten'}")
print(f"results: solution.py is now {source.stat().st_size} bytes")
print(f"worked: {done or '-'}")
print("did_not_work: -")
print("next: " + ("try the next mechanical shortening" if done else "a different algorithm would be needed"))
print("SESSION-REPORT-END")
