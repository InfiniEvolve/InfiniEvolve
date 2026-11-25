import math

import pytest

from infinievolve.config import parse_config
from infinievolve.core.round_selection import choose, parents, policy, probabilities
from infinievolve.core.schema import parse_declaration
from infinievolve.core.types import Node


def config(**kwargs):
    return parse_config({'campaign': {'adapter': 'fake', 'tasks': ['a'], **kwargs.pop('campaign', {})}, **kwargs}, name='test')


def node(index, score, **kwargs):
    return Node(f's{index:03d}', index, None, 'best', 'a', None, 'final', {}, True, 'score', score, [], **kwargs)


def metric(direction='max'):
    return parse_declaration({'metrics': {'score': {'direction': direction, 'scale': 'absolute'}}, 'rank_by': 'score'}).rank


def test_defaults_and_overrides():
    settings = config().for_task('a')
    assert settings.agents_per_round == 1 and settings.selection is None
    settings = config(campaign={'agents_per_round': 3}, task_overrides={'a': {'selection': 'genetic', 'population_size': 5}}).for_task('a')
    assert policy(settings)['population_size'] == 5
    assert policy(settings)['selection'] == 'genetic'


@pytest.mark.parametrize('campaign,strategy', [
    ({'agents_per_round': 0}, {}), ({'agents_per_round': 1.5}, {}), ({'agents_per_round': True}, {}),
    ({}, {'selection': 'bad'}), ({}, {'temperature': 0}), ({}, {'temperature': float('nan')}),
    ({}, {'crossover_rate': 2}), ({}, {'mutation_rate': -1}), ({}, {'population_size': 0}),
    ({}, {'population_size': 2, 'elite_count': 3}), ({'agents_per_round': 2}, {'start': 'explore'}),
])
def test_bad_configuration(campaign, strategy):
    with pytest.raises(ValueError):
        config(campaign=campaign, strategy=strategy)


def test_probabilities_equal_scores_direction_extremes():
    items = [node(0, -1e300), node(1, 1e300), node(2, 1e300)]
    p = probabilities(items, metric(), 1e-300)
    assert p == [0, .5, .5]
    assert probabilities(items, metric('min'), 1e-300) == [1, 0, 0]
    assert probabilities([node(1, 3), node(2, 3)], metric(), 1) == [.5, .5]
    assert math.isclose(sum(probabilities(items, metric(), 1)), 1)


def test_greedy_and_seeded_sampling_exclude_invalid():
    items = [node(0, 1), node(1, 3), node(2, 99, flags={'bad': {'level': 'hard'}})]
    p = policy(config(strategy={'selection': 'greedy'}).defaults)
    assert choose(p, items, metric(), 'a')['survivors'] == ['s001']
    p['selection'] = 'probabilistic'
    assert choose(p, items, metric(), 'a') == choose(p, list(reversed(items)), metric(), 'a')
    assert {r['session_id'] for r in choose(p, items, metric(), 'a')['candidates']} == {'s000', 's001'}


def test_genetic_elites_distinct_parents_and_rate_endpoints():
    items = [node(i, i + 1) for i in range(5)]
    items[0].artifact['fingerprint'] = 'same'
    items[1].artifact['fingerprint'] = 'same'
    p = policy(config(campaign={'agents_per_round': 3}, strategy={'selection': 'genetic', 'elite_count': 1, 'crossover_rate': 1, 'mutation_rate': 1}).defaults)
    result = choose(p, items, metric(), 'seed')
    assert len(result['survivors']) == 3 and result['survivors'][0] == 's004'
    assert not {'s000', 's001'} <= set(result['survivors'])
    plan = parents(p, items, metric(), 'seed')
    assert len(set(plan['parent_ids'])) == 2 and plan['mutation']
    p.update(crossover_rate=0, mutation_rate=0)
    plan = parents(p, items, metric(), 'seed')
    assert len(plan['parent_ids']) == 1 and not plan['mutation']
    p['crossover_rate'] = 1
    assert len(parents(p, items[:1], metric(), 'seed')['parent_ids']) == 1
