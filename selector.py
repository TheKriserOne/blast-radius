"""Select tests for each commit and choose the lowest-cost worker count."""

import json
import sys
from pathlib import Path

from impact import Impact

DATA = Path("/data")
APP = Path(__file__).resolve().parent
MAX_WORKERS = 4
WORKER_COST_MILLISECONDS = 2_000


def split(
    tests_by_duration: list[tuple[int, int]], worker_count: int
) -> tuple[list[list[int]], int]:
    """Longest test first, each onto the worker with the least to do so far."""
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for test_number, duration in tests_by_duration:
        lightest = loads.index(min(loads))
        workers[lightest].append(test_number)
        loads[lightest] += duration
    return [worker for worker in workers if worker], max(loads)


def split_two_workers(
    tests_by_duration: list[tuple[int, int]], total_duration: int
) -> tuple[list[list[int]], int]:
    """Find the largest subset load that fits within half the total duration."""
    target = total_duration // 2
    mask = (1 << (target + 1)) - 1

    # Bit i represents a reachable load of i milliseconds.
    reachable = 1
    previous_reachable = []
    for _, duration in tests_by_duration:
        previous_reachable.append(reachable)
        reachable |= (reachable << duration) & mask
        if reachable.bit_length() == target + 1:
            break

    first_load = reachable.bit_length() - 1
    remaining = first_load
    first_worker = set()
    for index in range(len(previous_reachable) - 1, -1, -1):
        # Include the current test if the remaining load was unreachable without it.
        if not ((previous_reachable[index] >> remaining) & 1):
            test_number, duration = tests_by_duration[index]
            first_worker.add(test_number)
            remaining -= duration

    workers = [
        [number for number, _ in tests_by_duration if number in first_worker],
        [number for number, _ in tests_by_duration if number not in first_worker],
    ]
    return workers, total_duration - first_load


def choose_workers(tests: list[dict]) -> list[list[int]]:
    if not tests:
        return []

    tests_by_duration = sorted(
        ((test["number"], round(test["seconds"] * 1_000)) for test in tests),
        key=lambda test: test[1],
        reverse=True,
    )
    total_duration = sum(duration for _, duration in tests_by_duration)
    longest_test = tests_by_duration[0][1]
    best_workers = [[number for number, _ in tests_by_duration]]
    best_cost = total_duration + WORKER_COST_MILLISECONDS

    for worker_count in range(2, min(MAX_WORKERS, len(tests)) + 1):
        workers, largest_load = split(tests_by_duration, worker_count)
        if worker_count == 2:
            lower_bound = max(longest_test, (total_duration + 1) // 2)
            if (
                largest_load > lower_bound
                and lower_bound + 2 * WORKER_COST_MILLISECONDS < best_cost
            ):
                workers, largest_load = split_two_workers(tests_by_duration, total_duration)

        cost = largest_load + WORKER_COST_MILLISECONDS * len(workers)
        if cost < best_cost:
            best_workers = workers
            best_cost = cost

    return best_workers


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    impact = Impact(tests, APP / "coverage.json.gz", APP / "baseline")
    by_number = {test["number"]: test for test in tests}
    schedules = {}
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            selected = impact.select(patch.read_bytes().decode("utf-8"))
        except UnicodeDecodeError:
            selected = None
        key = None if selected is None else frozenset(selected)
        if key not in schedules:
            candidates = tests if key is None else [by_number[number] for number in sorted(key)]
            schedules[key] = choose_workers(candidates)
        print(json.dumps({"commit": patch.stem, "workers": schedules[key]}))
    print(f"built {len(schedules)} schedules", file=sys.stderr)


if __name__ == "__main__":
    main()
