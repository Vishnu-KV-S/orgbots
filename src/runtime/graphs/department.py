"""Import every M1 actor graph, so the registry has them.

A single import site rather than four scattered ones. `known_graphs()` after
importing this module is the definitive answer to "what can this process run", which
is what the worker's startup check and T25's audit both need.
"""

from runtime.graphs import content, marketing_head, research
from runtime.graphs.registry import known_graphs

M1_GRAPHS = ("marketing_head@1", "research@1", "content@1")


def assert_registered() -> None:
    """Fail at startup rather than at first run.

    A worker that starts without a graph an actor's spec names does not find out
    until a cron fires days later, and by then the failure looks like a scheduling
    problem.
    """
    missing = sorted(set(M1_GRAPHS) - known_graphs())
    if missing:
        raise RuntimeError(f"M1 graphs not registered: {missing}")


__all__ = ["M1_GRAPHS", "assert_registered", "content", "marketing_head", "research"]
