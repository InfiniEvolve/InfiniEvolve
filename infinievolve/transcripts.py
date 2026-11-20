"""Reading what a coding agent wrote while it worked, for adapters.

Two machine-readable formats are understood, one JSON event per line: Claude Code's `stream-json` and
Codex's `--json`. Anything else is treated as plain text. Every function takes the path of the saved
output and never fails on a line it cannot read.
"""
from __future__ import annotations

import json
from pathlib import Path
import re


def events(path: Path):
    for line in Path(path).read_text(errors="replace").splitlines() if Path(path).is_file() else []:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def texts(path: Path) -> list[str]:
    """What the agent said, in order. For output that is not a stream of JSON events, the whole text."""
    said, structured = [], False
    for event in events(path):
        structured = True
        if event.get("type") == "assistant":   # Claude Code
            said += [item.get("text", "") for item in (event.get("message") or {}).get("content") or []
                     if isinstance(item, dict) and item.get("type") == "text"]
        elif event.get("type") == "result" and isinstance(event.get("result"), str):
            said.append(event["result"])
        elif event.get("type") == "item.completed":   # Codex
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                said.append(item["text"])
    if structured or not Path(path).is_file():
        return said
    return [Path(path).read_text(errors="replace")]


def tool_calls(path: Path) -> list[tuple[str, str]]:
    """(tool name, its input as text) for every tool call."""
    calls = []
    for event in events(path):
        for item in (event.get("message") or {}).get("content") or [] if isinstance(event.get("message"), dict) else []:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                calls.append((item.get("name", "?"), json.dumps(item.get("input", {}))))
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event.get("type") == "item.completed" and item.get("type") == "command_execution":
            calls.append(("command_execution", str(item.get("command", ""))))
    return calls


def usage(path: Path) -> dict:
    """Token totals, and the cost where the agent reports one. {} when nothing was reported."""
    totals: dict = {}
    for event in events(path):
        used = event.get("usage") if isinstance(event.get("usage"), dict) else None
        if event.get("type") == "result" and used:   # Claude Code's final event carries the whole session
            return {"input": used.get("input_tokens", 0), "output": used.get("output_tokens", 0),
                    "cache_creation_input": used.get("cache_creation_input_tokens", 0),
                    "cache_read_input": used.get("cache_read_input_tokens", 0),
                    "cost_usd": event.get("total_cost_usd"), "num_turns": event.get("num_turns")}
        if event.get("type") == "turn.completed" and used:   # Codex reports per turn
            for ours, theirs in (("input", "input_tokens"), ("output", "output_tokens"),
                                 ("cache_read_input", "cached_input_tokens")):
                totals[ours] = totals.get(ours, 0) + (used.get(theirs) or 0)
    return totals


def refusal(path: Path) -> tuple[str, float | None] | None:
    """(message, unix time to retry at) when Claude Code's service refused the session before the agent did
    anything: an error result and not one tool call. A session that worked and then ran into a limit is a
    real, shorter session, and gives None."""
    result, resets = None, []
    for event in events(path):
        if event.get("type") == "result":
            result = event
        resets += [int(value) for value in re.findall(r'"status":\s*"rejected",\s*"resetsAt":\s*(\d+)', json.dumps(event))]
    if result is None or not result.get("is_error") or tool_calls(path):
        return None
    if not (result.get("api_error") or result.get("api_error_status")):
        return None
    message = f"{result.get('api_error') or 'api error'}: {result.get('result') or ''}".strip()
    return message, float(min(resets)) if resets else None
