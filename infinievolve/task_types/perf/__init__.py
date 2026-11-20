"""Tasks ranked by how fast something runs.

A timing is noisy: the same code gives different numbers on another machine, at another moment, or twice
in a row, by several percent. Ranking a search by single timings lets luck pick the winners. What this
package offers against that, for any adapter of such a task:

    measure   timing a candidate in alternation with a reference on one machine (ABBA), and the statistics
              of it; a single file of standard library only, to send along with a job
    policy    what to declare, and when a measurement should not be believed
    wording   the task-specific sentences of the prompt
    adapter   a base class that puts the three together

None of it applies to a task whose metric is exact (a size, a count, a pass rate), whose evaluation cannot
be repeated, or whose two versions cannot be run side by side: those need no pairing, or cannot have it.
"""
