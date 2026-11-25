"""Configuration changes must also reach parallel-round confirmation barriers."""
from dataclasses import replace

import pytest

from .test_loop import events, run
from .test_rounds import setup, rounds


@pytest.mark.parametrize('selection', ['greedy', 'probabilistic', 'genetic'])
def test_confirmation_policy_changes_between_parallel_rounds(tmp_path, selection):
    store, config = setup(tmp_path, width=2, maximum=2, selection=selection,
                          evaluation={'confirm': {'samples': 1}}, rescore={'a': [2, 3]})
    state, adapter = run(store, config)
    assert not state['paused'] and adapter.rescored == {'a': 2}

    config = replace(config, defaults=replace(config.defaults, max_sessions=4,
                                             evaluation={'confirm': None}))
    state, adapter = run(store, config)
    assert not state['paused'] and adapter.rescored == {}
    assert len(events(store, 'confirmation_needed')) == 2
    assert rounds(store)[1]['confirmation_plan'] == []

    config = replace(config, defaults=replace(config.defaults, max_sessions=6,
                      evaluation={'confirm': {'samples': 1, 'aggregate': 'mean'}}))
    state, adapter = run(store, config)
    assert not state['paused'] and adapter.rescored == {'a': 2}
    assert len(events(store, 'confirmation_needed')) == 4
    assert len(events(store, 'confirm_changed')) == 2
    assert not events(store, 'declaration_mismatch')
    assert len(rounds(store)) == 3 and all('outcome' in r for r in rounds(store))
