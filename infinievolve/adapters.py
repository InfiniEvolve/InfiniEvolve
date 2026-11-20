"""Finding the adapter a campaign names.

`campaign.adapter` is looked up, in this order, as
    a name given to register_adapter() (tests register their stand-in this way),
    `local`, the built-in adapter (infinievolve.local),
    an entry point of the group `infinievolve.adapters` (how an installed package offers one),
    `package.module:Name`, imported,
    `path/to/file.py:Name`, loaded from that file (relative to the campaign directory, then to the
    current directory; a file in a directory with an __init__.py may import its neighbours relatively).
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
    """Load one file. Where its directory is a package (has an __init__.py), load it as part of that package,
    so that it can import the modules beside it relatively."""
    path = path.resolve()
    name = f"infinievolve_adapter_{abs(hash(str(path.parent)))}"
    package = (path.parent / "__init__.py").is_file()
    module_name = f"{name}.{path.stem}" if package else f"{name}_{path.stem}"
    if module_name not in sys.modules:
        if package and name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, path.parent / "__init__.py",
                                                          submodule_search_locations=[str(path.parent)])
            sys.modules[name] = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(sys.modules[name])
        if package:
            importlib.import_module(module_name)
        else:
            spec = importlib.util.spec_from_file_location(module_name, path)
            sys.modules[module_name] = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(sys.modules[module_name])
    return getattr(sys.modules[module_name], attribute)


def find_factory(name: str, root: Path | None = None) -> Callable[[CampaignConfig], Adapter]:
    if name in _FACTORIES:
        return _FACTORIES[name]
    if name == "local":   # the built-in adapter: every job a process on this machine
        from .local import LocalAdapter
        return LocalAdapter
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
