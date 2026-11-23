import json
from dataclasses import replace

import pytest

from infinievolve.core.archive import read_json
from infinievolve.core.loop import Scheduler
from .test_loop import campaign, run, nodes, events
from .fake_adapter import FakeAdapter


def setup(tmp_path, width=3, maximum=8, selection='greedy', **kwargs):
    store, config = campaign(tmp_path, {'a': list(range(1, 30))},
                             stop={'max_sessions': maximum, 'window_k': 99}, **kwargs)
    config = replace(config, max_parallel=width, defaults=replace(config.defaults, agents_per_round=width, selection=selection))
    return store, config


class Controlled(FakeAdapter):
    def __init__(self, config, reverse=False):
        super().__init__(config)
        self.batches = []
        self.reverse = reverse

    def poll(self, jobs, events):
        self.batches.append([j.key for j in jobs])
        # Complete only one job per poll; all other jobs remain running.
        return {(sorted(jobs, key=lambda j: j.key, reverse=self.reverse)[0]).key}


def rounds(store):
    return [read_json(p) for p in sorted((store.task('a').root / 'rounds').glob('r*.json'))]


def test_parallel_rounds_budget_and_frozen_history(tmp_path):
    store, config = setup(tmp_path)
    adapter = Controlled(config, reverse=True)
    state, _ = run(store, config, adapter)
    assert max(map(len, adapter.batches)) == 3
    assert state['sessions'] == 8 and state['tasks']['a']['stopped']
    assert [len(r['members']) for r in rounds(store)] == [3, 3, 2]
    specs = adapter.submitted
    assert [s.parent_id for s in specs] == ['s000'] * 3 + ['s003'] * 3 + ['s006'] * 2
    assert len({s.archive_md for s in specs[:3]}) == 1
    assert all('work of s001' not in s.archive_md for s in specs[:3])
    assert [s.round_id for s in specs] == ['r001'] * 3 + ['r002'] * 3 + ['r003'] * 2


def test_waves_do_not_see_completed_siblings(tmp_path):
    store, config = setup(tmp_path, maximum=3)
    config = replace(config, max_parallel=1)
    _, adapter = run(store, config)
    assert len({s.archive_md for s in adapter.submitted}) == 1
    assert [s.parent_id for s in adapter.submitted] == ['s000'] * 3


def test_genetic_donor_and_metadata(tmp_path):
    store, config = setup(tmp_path, width=2, maximum=4, selection='genetic', prompt={'carry': 'none', 'hide_scores': True})
    config = replace(config, defaults=replace(config.defaults, crossover_rate=1, mutation_rate=1))
    _, adapter = run(store, config)
    assert len(adapter.submitted) == 4
    for spec in adapter.submitted[2:]:
        assert len(spec.parent_ids) == 2 and spec.variation['mutation']
        assert '## Genetic variation' in spec.prompt
        assert f'work of {spec.parent_ids[1]}' in spec.prompt
        assert spec.archive_md == '' and '3.000' not in spec.prompt
        assert nodes(store)[spec.session_id].parent_ids == spec.parent_ids


def test_resume_mid_round_keeps_plans_and_updates_instruction(tmp_path, monkeypatch):
    store, config = setup(tmp_path, maximum=3)
    adapter = Controlled(config)
    scheduler = Scheduler(store, config, adapter, log=lambda _: None)
    running = {}
    scheduler._fill(running)
    loop, job = running.pop('a/s000')
    loop.finish(job)
    scheduler._fill(running)
    loop, job = running.pop('a/s001')
    loop.finish(job)
    saved = rounds(store)[0]
    # Persisted submitted jobs reattach even if configuration shrinks the width.
    config = replace(config, defaults=replace(config.defaults, agents_per_round=1, selection=None))
    state, _ = run(store, config, adapter)
    assert state['sessions'] == 3
    assert [s.session_id for s in adapter.submitted] == ['s001', 's002', 's003']
    assert rounds(store)[0]['members'] == saved['members']


def test_confirmations_are_completion_order_independent(tmp_path):
    outcomes = []
    for reverse in (False, True):
        directory = tmp_path / str(reverse)
        directory.mkdir()
        store, config = setup(directory, maximum=3, evaluation={'confirm': {'samples': 1}}, rescore={'a': [2, 3, 4]})
        adapter = Controlled(config, reverse)
        state, _ = run(store, config, adapter)
        assert not state['paused']
        assert adapter.rescored == {'a': 3}
        outcomes.append(rounds(store)[0]['outcome'])
    assert outcomes[0] == outcomes[1]


def test_hold_blocks_unsubmitted_round_members(tmp_path):
    store, config = setup(tmp_path, maximum=3)
    config = replace(config, max_parallel=1)
    adapter = Controlled(config)
    scheduler = Scheduler(store, config, adapter, log=lambda _: None)
    running = {}
    scheduler._fill(running)
    loop, job = running.pop('a/s000')
    loop.finish(job)
    scheduler._fill(running)
    loop, job = running.pop('a/s001')
    loop.finish(job)
    held = replace(config, defaults=replace(config.defaults, hold=True))
    state, _ = run(store, held, adapter)
    assert state['sessions'] == 1 and not state['tasks']['a']['stopped']
    assert rounds(store)[0].get('outcome') is None
    state, _ = run(store, config, adapter)
    assert state['sessions'] == 3


def test_failed_job_collects_siblings_and_resumes_same_slot(tmp_path):
    store, config = setup(tmp_path, maximum=3)
    config.options['values']['a'][1] = {'raise': 'failed', 'then': 2}
    adapter = Controlled(config)
    state, _ = run(store, config, adapter)
    assert 'a' in state['paused']
    assert set(nodes(store)) == {'s000', 's002', 's003'}
    state, _ = run(store, config, adapter)
    assert not state['paused'] and state['sessions'] == 3
    assert [s.session_id for s in adapter.submitted] == ['s001', 's002', 's003', 's001']
    assert adapter.submitted[0].parent_ids == adapter.submitted[-1].parent_ids


def test_refused_preparation_refreshes_instruction_not_history(tmp_path, monkeypatch):
    store, config = setup(tmp_path, maximum=3)
    adapter = Controlled(config)
    submit = adapter.submit_session
    attempts = []
    def reject(spec, directory, log):
        attempts.append(spec)
        if spec.session_id == 's002':
            raise RuntimeError('refused before submission')
        return submit(spec, directory, log)
    monkeypatch.setattr(adapter, 'submit_session', reject)
    state, _ = run(store, config, adapter)
    assert 'a' in state['paused'] and 's001' in nodes(store)
    monkeypatch.setattr(adapter, 'submit_session', submit)
    monkeypatch.setattr(adapter, 'instruction', lambda task: 'UPDATED INSTRUCTION')
    state, _ = run(store, config, adapter)
    assert not state['paused'] and state['sessions'] == 3
    new = next(s for s in adapter.submitted if s.session_id == 's002')
    assert 'UPDATED INSTRUCTION' in new.prompt
    assert new.archive_md == attempts[-1].archive_md and new.parent_ids == attempts[-1].parent_ids
    assert 'work of s001' not in new.archive_md


def test_void_sessions_do_not_consume_budget_or_reuse_ids(tmp_path):
    store, config = setup(tmp_path, width=2, maximum=3, declaration={'flags': {'empty': 'void'}})
    config.options['values']['a'][1] = {'value': 99, 'flags': {'empty': 'never worked'}}
    state, _ = run(store, config)
    assert not state['paused'] and state['sessions'] == 3
    assert list(nodes(store)) == ['s000', 's001', 's002', 's003', 's004']
    assert nodes(store)['s001'].void
    assert [len(r['members']) for r in rounds(store)] == [2, 2]


def test_invalid_round_falls_back_and_hard_flags_never_parent(tmp_path):
    store, config = setup(tmp_path, width=2, maximum=4)
    config.options['values']['a'][:3] = [{'value': 1, 'checks': {'ok': False}},
                                        {'value': 99, 'flags': {'bad': 'excluded'}},
                                        {'value': 98, 'checks': {'ok': False}}]
    state, adapter = run(store, config)
    assert not state['paused']
    assert [s.parent_id for s in adapter.submitted] == ['s000'] * 4
    assert rounds(store)[0]['outcome']['survivors'] == []


def test_configuration_transition_and_flagged_selected_parent(tmp_path):
    store, config = setup(tmp_path, width=2, maximum=2)
    run(store, config)
    config = replace(config, defaults=replace(config.defaults, selection='genetic', max_sessions=4, population_size=3))
    state, adapter = run(store, config)
    assert not state['paused']
    assert rounds(store)[1]['transition']['from']['selection'] == 'greedy'
    assert len(rounds(store)[1]['population']) == 3
    assert len(adapter.submitted) == 2


def test_fairness_global_ceiling_and_one_baseline(tmp_path):
    store, config = setup(tmp_path, width=3, maximum=3)
    config.options['values']['b'] = list(range(1, 30))
    adapter = Controlled(config)
    state, _ = run(store, config, adapter)
    assert state['sessions'] == 6 and not state['paused']
    assert max(map(len, adapter.batches)) == 3
    assert adapter.batches[0] == ['a/s000', 'b/s000']
    assert len([e for e in events(store, 'baseline_scored') if e['task'] == 'a']) == 1
    assert any(any(k.startswith('a/s00') for k in batch) and any(k.startswith('b/s00') for k in batch)
               for batch in adapter.batches[1:])


def test_duplicate_pooling_and_confirmations_are_order_independent(tmp_path):
    outcomes = []
    for reverse in (False, True):
        directory = tmp_path / str(reverse)
        directory.mkdir()
        store, config = setup(directory, maximum=3, evaluation={'confirm': {'samples': 1}}, rescore={'a': [2, 3, 4]})
        config.options['values']['a'][1:3] = [{'value': 4, 'work': b'same'}, {'value': 2, 'work': b'same'}]
        state, _ = run(store, config, Controlled(config, reverse))
        assert not state['paused']
        outcomes.append(rounds(store)[0]['outcome'])
    assert outcomes[0] == outcomes[1]


def test_unrenderable_genetic_donor_fails_explicitly(tmp_path):
    from infinievolve import artifacts
    class Opaque(artifacts.Text):
        name = 'unrenderable'
        def render(self, data):
            return None
    artifacts.register_artifact(Opaque())
    store, config = setup(tmp_path, width=2, maximum=4, selection='genetic', declaration={'artifact': 'unrenderable'})
    config = replace(config, defaults=replace(config.defaults, crossover_rate=1, mutation_rate=1))
    state, adapter = run(store, config)
    assert 'fully renderable' in state['paused']['a'] and len(adapter.submitted) == 2


def test_genetic_lineage_export_contains_both_edges(tmp_path):
    from infinievolve.export import lineage, dot, mermaid, export
    store, config = setup(tmp_path, width=2, maximum=4, selection='genetic')
    config = replace(config, defaults=replace(config.defaults, crossover_rate=1))
    run(store, config)
    graph = lineage(store.task('a'))
    child = next(n for n in graph['nodes'] if n['id'] == 's003')
    assert len(child['parents']) == 2 and child['round'] == 'r002'
    for parent in child['parents']:
        assert f'{parent} -> s003' in dot(graph)
        assert f'{parent} --> s003' in mermaid(graph)
    export(store, ['a'])
    assert 'parent_ids' in (store.exports_dir / 'nodes.csv').read_text().splitlines()[0]


def test_width_change_preserves_probabilistic_incumbent(tmp_path):
    store, config = setup(tmp_path, width=2, maximum=2, selection='probabilistic')
    config = replace(config, defaults=replace(config.defaults, temperature=1000))
    run(store, config)
    assert rounds(store)[0]['outcome']['survivors'] == ['s000']
    config = replace(config, defaults=replace(config.defaults, agents_per_round=3, max_sessions=3))
    state, adapter = run(store, config)
    assert not state['paused'] and adapter.submitted[0].parent_id == 's000'
    assert 'transition' not in rounds(store)[1]


def test_retained_genetic_duplicate_is_not_lost_to_old_archive_id(tmp_path):
    store, config = setup(tmp_path, width=1, maximum=3, selection='genetic')
    config.options['values']['a'][:4] = [
        {'value': 2, 'work': b'A'}, {'value': 3, 'work': b'B'},
        {'value': 6, 'work': b'A'}, {'value': 7, 'work': b'C'}]
    state, adapter = run(store, config)
    assert not state['paused']
    assert rounds(store)[1]['outcome']['survivors'] == ['s002']
    assert rounds(store)[2]['population'] == ['s002']
    assert adapter.submitted[2].parent_id == 's002'


def test_opening_new_round_clears_old_stop_status(tmp_path):
    store, config = setup(tmp_path, width=2, maximum=2)
    run(store, config)
    config = replace(config, defaults=replace(config.defaults, max_sessions=3))
    scheduler = Scheduler(store, config, Controlled(config), log=lambda _: None)
    running = {}
    scheduler._fill(running)
    state = scheduler.write_state()
    assert state['tasks']['a']['in_flight'] == ['s003']
    assert not state['tasks']['a']['stopped'] and state['tasks']['a']['stop_reason'] is None


def test_newly_excluded_parent_is_hidden_and_replanned_from_snapshot(tmp_path):
    store, config = setup(tmp_path, width=2, maximum=4, prompt={'hide_scores': True, 'carry': 'full'})
    config = replace(config, max_parallel=1)
    adapter = Controlled(config)
    scheduler = Scheduler(store, config, adapter, log=lambda _: None)
    running = {}
    for sid in ('s000', 's001', 's002', 's003'):
        scheduler._fill(running)
        loop, job = running.pop('a/' + sid)
        loop.finish(job)
    excluded = store.task('a').node('s002')
    excluded.flags['manual'] = {'level': 'hard', 'note': 'invalid measurement'}
    store.task('a').update_node(excluded)
    scheduler._fill(running)
    spec = adapter.submitted[-1]
    assert spec.session_id == 's004'
    assert 'work of s002' not in spec.archive_md
    assert 'Report and work withheld' in spec.archive_md
    assert spec.parent_id == 's001'
    assert 'work of s003' not in spec.archive_md
    assert rounds(store)[1]['revisions'][0]['reason'] == 'parent excluded by operator'


def test_crash_after_last_confirmation_outcome_reconciles_pool(tmp_path, monkeypatch):
    from infinievolve.core.rounds import RoundController
    store, config = setup(tmp_path, width=2, maximum=2,
                          evaluation={'confirm': {'samples': 1}}, rescore={'a': [1, 20]})
    config.options['values']['a'][1:3] = [{'value': 4, 'work': b'same'}, {'value': 4, 'work': b'same'}]
    adapter = Controlled(config)
    scheduler = Scheduler(store, config, adapter, log=lambda _: None)
    running = {}
    scheduler._fill(running)
    loop, job = running.pop('a/s000')
    loop.finish(job)
    scheduler._fill(running)
    for sid in ('s001', 's002'):
        loop, job = running.pop('a/' + sid)
        loop.finish(job)
    scheduler._fill(running)
    loop, job = running.pop('a/s001-c1')
    loop.finish(job)
    scheduler._fill(running)
    loop, job = running.pop('a/s002-c1')
    # Emulate a crash immediately after the final outcome is durable: no pool call.
    monkeypatch.setattr(loop, 'pool', lambda fingerprint: None)
    loop.finish(job)
    assert store.task('a').node('s001').rank_value != store.task('a').node('s002').rank_value
    state, _ = run(store, config, adapter)
    assert not state['paused']
    assert nodes(store)['s001'].rank_value == nodes(store)['s002'].rank_value == 4
    assert rounds(store)[0]['outcome']['survivors'] == ['s001']


def test_switch_to_legacy_after_partial_directory_keeps_ids_monotonic(tmp_path):
    store, config = setup(tmp_path, width=2, maximum=2)
    partial = store.task('a').session_dir('s001') / 'input'
    partial.mkdir(parents=True)
    (partial / 'prompt.md').write_text('interrupted preparation before spec was written')
    state, _ = run(store, config)
    assert not state['paused'] and list(nodes(store)) == ['s000', 's002', 's003']
    prior_input = (store.task('a').session_dir('s003') / 'input' / 'spec.json').read_bytes()
    legacy = replace(config, defaults=replace(config.defaults, agents_per_round=1, selection=None, max_sessions=3))
    adapter = Controlled(legacy)
    submit = adapter.submit_session
    def guard(spec, directory, log):
        assert not (directory / 'node.json').exists(), 'attempt to reuse an archived session ID'
        return submit(spec, directory, log)
    adapter.submit_session = guard
    state, adapter = run(store, legacy, adapter)
    assert not state['paused'] and state['sessions'] == 3
    assert [s.session_id for s in adapter.submitted] == ['s004']
    assert (store.task('a').session_dir('s003') / 'input' / 'spec.json').read_bytes() == prior_input
