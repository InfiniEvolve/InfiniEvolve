"""Artifact kinds: the form in which a session's work is archived.

The framework treats a session's work as opaque bytes. The adapter produces those bytes when a session
ends and restores them as the starting point of a later session; an artifact kind tells the framework
the rest:

    suffix             the file extension the work is archived under
    summary(data)      a small JSON-able description, kept on the node
    describe(summary)  that description as one line for the prompt
    fingerprint(data)  a string equal for two versions exactly when they are the same work, or None when
                       equal work cannot be recognised from the bytes (then nothing is called a duplicate)
    render(data)       the work as Markdown for the full archive, or None when it cannot be shown

Two kinds ship here: `patch` (a unified diff against the task's baseline) and `text` (one document).
Register another with register_artifact().
"""
from __future__ import annotations

import hashlib
from typing import Protocol

FILE_LIST_LIMIT = 40   # a longer file list is cut to this many names and a count


class ArtifactKind(Protocol):
    name: str
    suffix: str

    def summary(self, data: bytes) -> dict: ...
    def describe(self, summary: dict) -> str: ...
    def fingerprint(self, data: bytes) -> str | None: ...
    def render(self, data: bytes) -> str | None: ...


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Patch:
    """A unified diff against the baseline, as `git diff` writes it."""
    name, suffix = "patch", ".patch"

    def summary(self, data: bytes) -> dict:
        files, added, removed = [], 0, 0
        for line in data.decode("utf-8", errors="replace").splitlines():
            if line.startswith("diff --git a/"):
                files.append(line[len("diff --git a/"):].split(" b/", 1)[0])
            elif line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return {"files": files, "added": added, "removed": removed}

    def describe(self, summary: dict) -> str:
        files = list(summary.get("files") or [])
        if not files:
            return "no changes against the baseline"
        names = ", ".join(files[:FILE_LIST_LIMIT])
        if len(files) > FILE_LIST_LIMIT:
            names += f", ... ({len(files)} files in total)"
        return f"changes {names} (+{summary.get('added', 0)}/-{summary.get('removed', 0)} lines against the baseline)"

    def fingerprint(self, data: bytes) -> str | None:
        return _sha256(data)

    def render(self, data: bytes) -> str | None:
        text = data.decode("utf-8", errors="replace").rstrip()
        return f"Changes against the baseline:\n\n```diff\n{text}\n```" if text else None


class Text:
    """One text document, archived whole."""
    name, suffix = "text", ".txt"

    def summary(self, data: bytes) -> dict:
        text = data.decode("utf-8", errors="replace")
        return {"characters": len(text), "lines": len(text.splitlines())}

    def describe(self, summary: dict) -> str:
        return f"{summary.get('lines', 0)} lines, {summary.get('characters', 0)} characters"

    def fingerprint(self, data: bytes) -> str | None:
        return _sha256(data)

    def render(self, data: bytes) -> str | None:
        text = data.decode("utf-8", errors="replace").rstrip()
        fence = "````" if "```" in text else "```"
        return f"{fence}text\n{text}\n{fence}" if text else None


DEFAULT = "patch"
_KINDS: dict[str, ArtifactKind] = {"patch": Patch(), "text": Text()}


def register_artifact(kind: ArtifactKind) -> None:
    _KINDS[kind.name] = kind


def artifact_kind(name: str | None) -> ArtifactKind:
    """The kind of that name; the default kind for "" or None."""
    try:
        return _KINDS[name or DEFAULT]
    except KeyError:
        raise ValueError(f"unknown artifact kind {name!r}; known: {sorted(_KINDS)}") from None
