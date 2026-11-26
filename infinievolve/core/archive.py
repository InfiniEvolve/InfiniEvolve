"""On-disk layout of a campaign.

    <campaign>/
      config.yaml  provenance.json  events.jsonl  state.json  summary.{json,md}
      tasks/<task>/
        task.json                    the declaration this lineage is fixed to
        sessions/<sid>/{input/, raw/, derived/, job.json, node.json, manifest.json}
        sessions/<sid>/settle.json   archived, but not yet pooled and checked for a repeat scoring
        archive/{nodes/<sid>.json, artifacts/<sid><suffix>, archive.jsonl, matrix.{json,md}}
      exports/

sessions/ holds everything a session produced and is written once; archive/ is the compact view the
task's next agent reads; state.json, summary.* and archive/matrix.* can be rebuilt from the node.json
files and events.jsonl at any time.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .events import EventLog
from .schema import Declaration, parse_declaration
from .types import Node

COMPACT_FIELDS = ("session_id", "index", "parent_id", "start_strategy", "chosen_label", "artifact", "valid",
                  "rank_metric", "rank_value", "scores", "report", "error", "flags", "flags_cleared",
                  "round_id", "parent_ids", "variation")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str) + "\n")
    tmp.replace(path)


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


class CampaignStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.events = EventLog(self.root / "events.jsonl")

    @property
    def config_path(self) -> Path:
        return self.root / "config.yaml"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    def task(self, name: str) -> "TaskStore":
        return TaskStore(self.root / "tasks" / name, self.events.for_task(name), name)


class TaskStore:
    """One task's lineage inside a campaign."""

    def __init__(self, root: Path, events: EventLog, name: str) -> None:
        self.root = Path(root)
        self.events = events
        self.name = name

    # -- paths ---------------------------------------------------------------
    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def meta_path(self) -> Path:
        """Per-task facts fixed for the life of the lineage: its declaration."""
        return self.root / "task.json"

    def session_dir(self, sid: str) -> Path:
        return self.root / "sessions" / sid

    def node_path(self, sid: str) -> Path:
        return self.session_dir(sid) / "node.json"

    def artifact_path(self, sid: str) -> Path | None:
        found = sorted((self.archive_dir / "artifacts").glob(f"{sid}.*"))
        return found[0] if found else None

    # -- declaration ---------------------------------------------------------
    def declaration(self) -> Declaration | None:
        """The declaration fixed for this lineage; None before its baseline is archived."""
        meta = read_json(self.meta_path)
        return parse_declaration(meta["declaration"]) if meta else None

    # -- nodes ---------------------------------------------------------------
    def nodes(self) -> list[Node]:
        paths = sorted((self.root / "sessions").glob("*/node.json"))
        return sorted((Node.from_dict(json.loads(path.read_text())) for path in paths), key=lambda node: node.index)

    def node(self, sid: str) -> Node | None:
        data = read_json(self.node_path(sid))
        return Node.from_dict(data) if data else None

    def artifact(self, sid: str | None) -> bytes:
        path = self.artifact_path(sid) if sid else None
        return path.read_bytes() if path else b""

    def _compact(self, node: Node) -> dict:
        compact = {key: getattr(node, key) for key in COMPACT_FIELDS}
        compact["timing"] = {key: node.timing.get(key) for key in ("agent_seconds", "wall_seconds")}
        return compact

    def archive_node(self, node: Node, artifact: bytes, suffix: str) -> None:
        """Write the node once into sessions/ and its compact copy and work into archive/."""
        if self.node_path(node.session_id).is_file():
            raise FileExistsError(f"{node.session_id} is already archived")
        directory = self.archive_dir / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{node.session_id}{suffix}").write_bytes(artifact)
        compact = self._compact(node)
        write_json(self.archive_dir / "nodes" / f"{node.session_id}.json", compact)
        with (self.archive_dir / "archive.jsonl").open("a") as stream:
            stream.write(json.dumps(compact, sort_keys=True, default=str) + "\n")
        write_json(self.node_path(node.session_id), node.to_dict())  # last: its presence marks the session done

    def update_node(self, node: Node) -> None:
        """Rewrite an archived node (a flag, a later measurement); the event log records why."""
        if not self.node_path(node.session_id).is_file():
            raise FileNotFoundError(f"{node.session_id} is not archived")
        write_json(self.archive_dir / "nodes" / f"{node.session_id}.json", self._compact(node))
        write_json(self.node_path(node.session_id), node.to_dict())

    def in_flight(self) -> list[str]:
        """Sessions whose job was submitted but whose result is not archived yet."""
        return [path.parent.name for path in sorted((self.root / "sessions").glob("*/job.json"))
                if not (path.parent / "node.json").is_file()]

    def write_manifest(self, sid: str) -> dict:
        root = self.session_dir(sid)
        skipped = ("manifest.json", "settle.json")   # settle.json is gone once the session is settled
        manifest = {str(path.relative_to(root)): sha256_file(path)
                    for path in sorted(root.rglob("*")) if path.is_file() and path.name not in skipped}
        write_json(root / "manifest.json", manifest)
        return manifest
