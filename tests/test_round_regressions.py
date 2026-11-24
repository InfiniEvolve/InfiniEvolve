"""Regressions for round-boundary selection and recovery, using scripted adapters."""
from dataclasses import replace

import pytest

from infinievolve.core.archive import write_json
from infinievolve.core.loop import Scheduler
from infinievolve.core.rounds import records, round_state
from .test_loop import run
from .test_rounds import setup, Controlled, rounds


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('direction,values,repeat,pooled', [
    ('max', [10, 2, 7], 1, 6),
    ('min', [2, 10, 5], 11, 6),
])
def test_new_best_after_pooling_is_confirmed(tmp_path, reverse, direction, values, repeat, pooled):
    store, config = setup(tmp_path, width=2, maximum=2,
                          declaration={'metrics': {'score': {'direction': direction, 'scale': 'absolute'}}},
                          evaluation={'confirm': {'samples': 1}}, rescore={'a': [repeat]})
    config.options['values']['a'][:3] = [
        {'value': values[0], 'work': b'A'}, {'value': values[1], 'work': b'A'},
        {'value': values[2], 'work': b'B'}]
    state, adapter = run(store, config, Controlled(config, reverse))
    assert not state['paused']
    assert adapter.rescored == {'a': 1}
    assert state['tasks']['a']['best'] == pooled
    assert rounds(store)[0]['outcome']['survivors'] == ['s000']
    # Scoring uses updated values, but the agents' frozen historical inputs stay intact.
    assert rounds(store)[0]['snapshot'][0]['rank_value'] == values[0]


def test_sequential_control_confirms_same_new_best(tmp_path):
    store, config = setup(tmp_path, width=1, maximum=2,
                          evaluation={'confirm': {'samples': 1}}, rescore={'a': [1]})
    config = replace(config, defaults=replace(config.defaults, selection=None))
    config.options['values']['a'][:3] = [
        {'value': 10, 'work': b'A'}, {'value': 2, 'work': b'A'}, {'value': 7, 'work': b'B'}]
    state, adapter = run(store, config)
    assert adapter.rescored == {'a': 1} and state['tasks']['a']['best'] == 6


@pytest.mark.parametrize('selection', ['greedy', 'genetic'])
def test_replanned_parent_participates_in_survivor_selection_after_restart(tmp_path, selection):
    store, config = setup(tmp_path, width=2, maximum=4, selection=selection)
    config = replace(config, max_parallel=1,
                     defaults=replace(config.defaults, population_size=1, elite_count=1))
    config.options['values']['a'][:5] = [1, 2, 3, 1, 1]
    adapter = Controlled(config)
    scheduler = Scheduler(store, config, adapter, log=lambda _: None)
    running = {}
    for sid in ('s000', 's001', 's002', 's003'):
        scheduler._fill(running)
        loop, job = running.pop('a/' + sid)
        loop.finish(job)
    parent = store.task('a').node('s002')
    parent.flags['manual'] = {'level': 'hard', 'note': 'bad work'}
    store.task('a').update_node(parent)
    scheduler._fill(running)
    assert adapter.submitted[-1].parent_id == 's001'
    loop, job = running.pop('a/s004')
    loop.finish(job)
    # Restart after all results are archived but before round selection is committed.
    state, _ = run(store, config, adapter)
    assert not state['paused']
    assert rounds(store)[1]['outcome']['survivors'] == ['s001']
    assert {n['session_id'] for n in rounds(store)[1]['outcome']['candidates']} == {'s001', 's003', 's004'}


def test_round_records_and_status_remain_chronological_after_999(tmp_path):
    store, _ = setup(tmp_path)
    for i in range(1, 1001):
        write_json(store.task('a').root / 'rounds' / f'r{i:03d}.json',
                   {'id': f'r{i:03d}', 'policy': {'selection': 'greedy'}, 'outcome': {}, 'members': []})
    assert records(store.task('a'))[-1]['id'] == 'r1000'
    assert round_state(store.task('a'))['id'] == 'r1000'
