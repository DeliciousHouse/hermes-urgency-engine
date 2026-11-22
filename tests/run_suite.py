"""Run the unchanged discovered suite, optionally in exhaustive CI partitions."""
import argparse
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SLOW = 'test_kanban_reconciliation.ReconciliationTests.test_ten_thousand_sources_finish_after_restart_and_key_churn'


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def partition(tests, name):
    if name not in ('all', 'fast', 'slow'):
        raise ValueError('Unknown partition')
    if sum(test.id() == SLOW for test in tests) != 1:
        raise ValueError('The disk-backed restart case must be discovered exactly once')
    if name == 'all':
        return tests
    return [test for test in tests if (test.id() == SLOW) == (name == 'slow')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--partition', choices=('all', 'fast', 'slow'), default='all')
    parser.add_argument('--list', action='store_true', help='Print selected test IDs without running')
    args = parser.parse_args()
    tests = list(cases(unittest.defaultTestLoader.discover(str(ROOT / 'tests'))))
    selected = partition(tests, args.partition)
    print(f'Partition {args.partition}: {len(selected)} of {len(tests)} discovered tests', flush=True)
    if args.list:
        for test in selected:
            print(test.id())
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    return int(not (result.wasSuccessful() and result.testsRun == len(selected) and result.testsRun and not result.skipped))


if __name__ == '__main__':
    raise SystemExit(main())
