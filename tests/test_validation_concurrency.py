import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from scripts import v12_bitget_live_validation as live


class PageCounterConcurrencyTests(unittest.TestCase):
    def test_page_request_counts_are_isolated_per_worker(self):
        barrier = Barrier(2)

        def count_pages(count):
            live._PAGE_COUNTER.calls = 0
            for _ in range(count):
                live._counted_page()
            barrier.wait(timeout=3)
            return live._PAGE_COUNTER.calls

        with patch.object(live, "_ORIGINAL_PAGE", return_value=None):
            with ThreadPoolExecutor(max_workers=2) as pool:
                counts = list(pool.map(count_pages, (2, 5)))

        self.assertEqual(counts, [2, 5])


if __name__ == "__main__":
    unittest.main()
