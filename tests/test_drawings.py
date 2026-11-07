import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from chengchuan.application.drawings import DrawingBook
from chengchuan.domain.drawings import DrawingPoint, price_difference
from chengchuan.domain.models import SHANGHAI
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository


class DrawingTests(unittest.TestCase):
    def point(self, price):
        return DrawingPoint(datetime(2026, 11, 5, 15, tzinfo=SHANGHAI), price)

    def test_signed_difference_uses_first_line_even_when_lines_cross(self):
        self.assertEqual(price_difference(10, 12), (2, 20))
        self.assertEqual(price_difference(10, 8), (-2, -20))
        self.assertEqual(price_difference(10, 10), (0, 0))
        self.assertAlmostEqual(price_difference(12, 10)[1], -100 / 6)
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                price_difference(value, 10)

    def test_prices_and_timestamp_anchors_survive_reopen_and_contexts_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = SqliteWorkspaceRepository(Path(directory) / "workspace.sqlite")
            book = DrawingBook(workspace)
            a = book.add("sz002318", "day", "price_range", (self.point(10), self.point(12)))
            b = book.add("sz002318", "minute", "horizontal", (self.point(9),))
            book.flush()
            reopened = DrawingBook(workspace)
            self.assertEqual(reopened.for_chart(a.context), (a,))
            self.assertEqual(reopened.for_chart(b.context), (b,))
            reopened.remove((a.id,))
            reopened.flush()
            self.assertEqual(tuple(DrawingBook(workspace).items.values()), (b,))

    def test_capacity_is_bounded_and_unrecognized_schema_is_not_overwritten(self):
        book = DrawingBook()
        book.max_per_chart = 2
        for _ in range(2):
            book.add("sz002318", "day", "horizontal", (self.point(10),))
        with self.assertRaises(ValueError):
            book.add("sz002318", "day", "horizontal", (self.point(10),))

        class FutureWorkspace:
            def load(self, name):
                return {"schema": 99, "items": []}

            def save(self, *args):
                raise AssertionError("不应覆盖更高版本的档案")

        book = DrawingBook(FutureWorkspace())
        self.assertTrue(book.read_only)
        with self.assertRaises(ValueError):
            book.add("sz002318", "day", "horizontal", (self.point(10),))
        book.flush()


if __name__ == "__main__":
    unittest.main()
