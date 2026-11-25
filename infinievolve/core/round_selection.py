"""Pure, deterministic selection for round incumbents and genetic populations.

Fitness uses normalized score ranks, not score magnitudes. All RNG decisions and
probabilities are returned as data so a saved round can explain its lineage.
"""
from __future__ import annotations

import math
import random

from .selection import eligible
from .types import ROOT_ID


def policy(settings) -> dict:
    return {"selection": settings.selection or "greedy", "temperature": settings.temperature,
            "population_size": settings.population_size or settings.agents_per_round,
            "elite_count": settings.elite_count, "crossover_rate": settings.crossover_rate,
            "mutation_rate": settings.mutation_rate, "seed": settings.seed}


def ordered(nodes, metric):
    # Never negate scores (which may have arbitrary numeric scales).
    return sorted(sorted(eligible(nodes, metric), key=lambda n: n.index),
                  key=lambda n: n.rank_value, reverse=metric.direction == "max")


def unique(nodes, metric):
    seen, result = set(), []
    for node in ordered(nodes, metric):
        key = node.artifact.get("fingerprint") or node.session_id
        if key not in seen:
            result.append(node)
            seen.add(key)
    return result


def probabilities(nodes, metric, temperature):
    if not nodes:
        return []
    values = sorted({n.rank_value for n in nodes}, reverse=metric.direction == "min")
    positions = {v: i / max(1, len(values) - 1) for i, v in enumerate(values)}
    # Subtract the maximum before division: even subnormal temperatures are safe.
    high = max(positions.values())
    weights = [math.exp((positions[n.rank_value] - high) / temperature) for n in nodes]
    total = math.fsum(weights)
    return [w / total for w in weights]


def draw(nodes, metric, temperature, rng):
    probs = probabilities(nodes, metric, temperature)
    value, cumulative = rng.random(), 0.0
    chosen = nodes[-1]
    for node, probability in zip(nodes, probs):
        cumulative += probability
        if value < cumulative:
            chosen = node
            break
    return chosen, {"draw": value, "selected": chosen.session_id,
                    "candidates": [{"session_id": n.session_id, "value": n.rank_value, "probability": p}
                                   for n, p in zip(nodes, probs)]}


def choose(config, nodes, metric, seed):
    pool = sorted(eligible(nodes, metric), key=lambda n: n.index)
    result = {"seed": seed, "selection": config["selection"], "survivors": [], "draws": [],
              "candidates": [{"session_id": n.session_id, "value": n.rank_value} for n in pool]}
    if not pool:
        result["fallback"] = ROOT_ID
        return result
    if config["selection"] == "greedy":
        result["survivors"] = [ordered(pool, metric)[0].session_id]
        return result
    rng = random.Random(seed)
    if config["selection"] == "probabilistic":
        winner, audit = draw(pool, metric, config["temperature"], rng)
        result.update(survivors=[winner.session_id], candidates=audit["candidates"], draws=[audit])
        return result
    pool = unique(pool, metric)
    keep = pool[:config["elite_count"]]
    result["elites"] = [n.session_id for n in keep]
    pool = [n for n in pool if n not in keep]
    while pool and len(keep) < config["population_size"]:
        winner, audit = draw(pool, metric, config["temperature"], rng)
        keep.append(winner)
        pool.remove(winner)
        result["draws"].append(audit)
    result["survivors"] = [n.session_id for n in keep]
    return result


def parents(config, population, metric, seed):
    pool = sorted(unique(population, metric), key=lambda n: n.index)
    result = {"seed": seed, "parent_ids": [], "mutation": False, "draws": []}
    if config["selection"] != "genetic":
        result["parent_ids"] = [pool[0].session_id] if pool else [ROOT_ID]
        return result
    rng = random.Random(seed)
    if pool:
        primary, audit = draw(pool, metric, config["temperature"], rng)
        result["parent_ids"].append(primary.session_id)
        result["draws"].append(audit)
        pool.remove(primary)
    else:
        result["parent_ids"] = [ROOT_ID]
    result["crossover_draw"] = rng.random()
    if pool and result["crossover_draw"] < config["crossover_rate"]:
        secondary, audit = draw(pool, metric, config["temperature"], rng)
        result["parent_ids"].append(secondary.session_id)
        result["draws"].append(audit)
    result["mutation_draw"] = rng.random()
    result["mutation"] = result["mutation_draw"] < config["mutation_rate"]
    return result
