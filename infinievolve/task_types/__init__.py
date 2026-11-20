"""What kinds of task have in common, between the framework and the adapters of single benchmarks.

The framework knows nothing about any kind of task, and an adapter knows one benchmark. What every
benchmark of one kind shares (how its results are measured so that they can be trusted, what is declared
about them, how the agent is told) lives here, one package per kind.

    perf    tasks ranked by how fast something runs: paired timing, repeat scoring, noisy-result checks
"""
