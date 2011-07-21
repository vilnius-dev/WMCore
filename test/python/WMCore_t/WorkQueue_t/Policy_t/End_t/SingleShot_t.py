#!/usr/bin/env python
"""
    WorkQueue.Policy.End.SingleShot tests
"""




import unittest
import math
from functools import partial
from WMCore.WorkQueue.Policy.End.SingleShot import SingleShot
from WMCore.WorkQueue.DataStructs.WorkQueueElement import WorkQueueElement as WQE


class SingleShotTestCase(unittest.TestCase):

    def setUp(self):
        """Create workflow stuff"""
        self.policy = partial(SingleShot, SuccessThreshold = 0.9)
        self.strict_policy = partial(SingleShot)

        # ones i made earlier
        self.parent = WQE(); self.parent.id = 1
        self.available = WQE(Status = 'Available', ParentQueueId = 1)
        self.acquired = WQE(Status = 'Acquired', ParentQueueId = 1)
        self.negotiating = WQE(Status = 'Negotiating', ParentQueueId = 1)
        self.done = WQE(Status = 'Done', PercentComplete = 100, PercentSuccess = 100, ParentQueueId = 1)
        self.failed = WQE(Status = 'Failed', PercentComplete = 100, PercentSuccess = 0, ParentQueueId = 1)

    def tearDown(self):
        pass


    def testSuccessThreshold(self):
        """Check threshold for success"""
        # range doesn't work with decimals?
        # Source: http://code.activestate.com/recipes/66472/
        def frange4(end, start = 0, inc = 0, precision = 1):
            """A range function that accepts float increments."""
            if not start:
                start = end + 0.0
                end = 0.0
            else: end += 0.0

            if not inc:
                inc = 1.0
            count = int(math.ceil((start - end) / inc))

            L = [None] * count

            L[0] = end
            for i in (xrange(1, count)):
                L[i] = L[i - 1] + inc
            return L

        # create dict with appropriate percentage of success/failures
        elements = {}
        for i in range(0, 100, 5):
            elements[i / 100.] = [self.done] * i + [self.failed] * (100 - i)

        # go through range, checking correct status for entire pre-seeded dict
        # be careful of rounding errors here
        for threshold in frange4(0., 1., 0.1):
            policy = partial(SingleShot, SuccessThreshold = threshold)
            for value, items in elements.items():
                if value >= threshold:
                    self.assertEqual(policy()(items, [self.parent])[0]['Status'], 'Done')
                else:
                    self.assertEqual(policy()(items, [self.parent])[0]['Status'], 'Failed')


if __name__ == '__main__':
    unittest.main()
