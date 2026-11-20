"""Which machines not to measure on: a ledger of the ones that gave measurements nobody could reproduce.

A measurement that looks wrong and comes out normal when taken again elsewhere says something about the
machine it was first taken on. Once is a strike; a second strike, from another measurement, takes the
machine out of use. A machine that is plainly broken can be excluded at once.

The ledger is a YAML file of your own (machine names are yours, and stay out of any shared repository):

    strikes:  [{host, reason, since, evidence}]
    excluded: [{host, reason, since, evidence}]

Remove an entry to give a machine another chance.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml


class HostLedger:
    def __init__(self, path) -> None:
        self.path = Path(path)

    def _read(self) -> dict:
        data = (yaml.safe_load(self.path.read_text()) if self.path.is_file() else None) or {}
        return {"strikes": list(data.get("strikes") or []), "excluded": list(data.get("excluded") or [])}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(yaml.safe_dump(data, sort_keys=False, width=120, allow_unicode=True))
        temporary.replace(self.path)

    def excluded(self) -> list:
        """The machines not to use, read now, so that an edit of the file applies at once."""
        return [str(entry["host"]) for entry in self._read()["excluded"] if entry.get("host")]

    def exclude(self, host: str, reason: str, evidence: dict | None = None) -> bool:
        """Take a machine out of use at once. False when it already is."""
        data = self._read()
        if not host or host in [entry.get("host") for entry in data["excluded"]]:
            return False
        data["strikes"] = [entry for entry in data["strikes"] if entry.get("host") != host]
        data["excluded"].append({"host": host, "reason": reason, "since": date.today().isoformat(),
                                 "evidence": evidence or {}})
        self._write(data)
        return True

    def strike(self, host: str, reason: str, evidence: dict | None = None) -> str | None:
        """Record that a measurement on this machine did not hold up elsewhere. Returns "strike" for a first
        one, "excluded" when it was the second from a different measurement, None when nothing changed."""
        data, evidence = self._read(), evidence or {}
        if not host or host in [entry.get("host") for entry in data["excluded"]]:
            return None
        earlier = [entry for entry in data["strikes"] if entry.get("host") == host]
        if any(entry.get("evidence") == evidence for entry in earlier):
            return None   # the same measurement, counted already
        today = date.today().isoformat()
        if not earlier:
            data["strikes"].append({"host": host, "reason": reason, "since": today, "evidence": evidence})
            self._write(data)
            return "strike"
        data["strikes"] = [entry for entry in data["strikes"] if entry.get("host") != host]
        data["excluded"].append({"host": host, "reason": f"{reason} (second time; first: {earlier[0].get('reason')})",
                                 "since": today, "evidence": [earlier[0].get("evidence"), evidence]})
        self._write(data)
        return "excluded"
