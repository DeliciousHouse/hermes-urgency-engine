"""Bounded deterministic list scheduling for a complete dependency graph."""
from graphlib import CycleError, TopologicalSorter
from heapq import heappop, heappush
from math import isfinite


MAX_NODES = 10000
MAX_EDGES = 50000


def schedule(parents, durations, slots):
    """Return makespan with lexicographic ready tasks and simultaneous releases."""
    if (len(parents) > MAX_NODES or sum(map(len, parents.values())) > MAX_EDGES
            or type(slots) is not int or not 1 <= slots <= 64
            or set(parents) != set(durations)):
        raise ValueError('invalid schedule')
    if any(parent not in parents for deps in parents.values() for parent in deps):
        raise ValueError('dependency outside project')
    if any(type(value) not in (int, float) or not isfinite(value) or value < 0
           for value in durations.values()):
        raise ValueError('invalid duration')
    sorter = TopologicalSorter(parents)
    try:
        sorter.prepare()
    except CycleError:
        raise ValueError('dependency cycle') from None
    ready, running = [], []
    for task in sorter.get_ready():
        heappush(ready, task)
    now = 0
    while ready or running:
        while ready and len(running) < slots:
            task = heappop(ready)
            heappush(running, (now + durations[task], task))
        now = running[0][0]
        done = []
        while running and running[0][0] == now:
            done.append(heappop(running)[1])
        sorter.done(*done)
        for task in sorter.get_ready():
            heappush(ready, task)
    return now
