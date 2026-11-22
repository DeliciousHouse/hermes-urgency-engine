"""protects=complete CI partition; fails_when=tests lost or duplicated;
why_new=parallel Windows lane; seam=unittest discovery; no_claim=hosted timings.
"""
from collections import Counter
import unittest


class PartitionTests(unittest.TestCase):
    def test_discovery_partitions_are_disjoint_and_cover_every_test(self):
        from run_suite import cases, partition, SLOW
        discovered = list(cases(unittest.defaultTestLoader.discover('tests')))
        fast = partition(discovered, 'fast')
        slow = partition(discovered, 'slow')
        self.assertEqual([t.id() for t in slow], [SLOW])
        self.assertEqual(Counter(t.id() for t in fast + slow), Counter(t.id() for t in discovered))
        self.assertFalse(set(t.id() for t in fast) & set(t.id() for t in slow))
        self.assertEqual(partition(discovered, 'all'), discovered)

    def test_missing_or_duplicate_slow_case_is_a_named_refusal(self):
        from run_suite import partition, SLOW
        class Named:
            def id(self):
                return SLOW
        for tests in ([], [Named(), Named()]):
            with self.subTest(count=len(tests)), self.assertRaisesRegex(ValueError, 'exactly once'):
                partition(tests, 'fast')

    def test_unknown_partition_is_refused(self):
        from run_suite import partition
        with self.assertRaisesRegex(ValueError, 'Unknown partition'):
            partition([], 'typo')
