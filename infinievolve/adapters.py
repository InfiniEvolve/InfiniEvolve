"""Finding the adapter a campaign names.

`campaign.adapter` is looked up, in this order, as
    a name given to register_adapter() (tests register their stand-in this way),
    an entry point of the group `infinievolve.adapters` (how an installed package offers one),
    `package.module:Name`, imported,
    `path/to/file.py:Name`, loaded from that file (relative to the campaign directory, then to the
    current directory).
What is found is called with the campaign's config and must return the adapter.
"""
from __future__ import annotations

import importlib
import importlib.util
from importlib.metadata import entry_points
from pathlib import Path
import sys
from typing import Callable

from .config import CampaignConfig
from .core.adapter import Adapter

GROUP = "infinievolve.adapters"
_FACTORIES: dict[str, Callable[[CampaignConfig], Adapter]] = {}


def register_adapter(name: str, factory: Callable[[CampaignConfig], Adapter]) -> None:
    _FACTORIES[name] = factory


def _from_file(path: Path, attribute: str):
    name = f"infinievolve_adapter_{abs(hash(str(path.resolve())))}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return getattr(sys.modules[name], attribute)


def find_factory(name: str, root: Path | None = None) -> Callable[[CampaignConfig], Adapter]:
    if name in _FACTORIES:
        return _FACTORIES[name]
    for entry in entry_points(group=GROUP):
        if entry.name == name:
            return entry.load()
    if ":" in name:
        target, attribute = name.rsplit(":", 1)
        if target.endswith(".py"):
            for base in ([root] if root else []) + [Path.cwd()]:
                if (base / target).is_file():
                    return _from_file(base / target, attribute)
            raise ValueError(f"adapter file {target} not found (looked in {root or '.'} and the current directory)")
        return getattr(importlib.import_module(target), attribute)
    raise ValueError(f"unknown adapter {name!r}: not registered, not an entry point of {GROUP}, and not of "
                     "the form module:Name or file.py:Name")


def make_adapter(config: CampaignConfig) -> Adapter:
    return find_factory(config.adapter, config.root)(config)
