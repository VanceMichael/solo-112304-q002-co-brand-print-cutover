import unittest

from source_fixture import load


class ExchangeInputs(unittest.TestCase):
    def test_related_source_records(self):
        exchange = load()
        initial = exchange["initial"]
        changes = exchange["changes"]
        boxes = initial["/factory/cartons"]["body"]["cartons"]
        notice = changes[0]["routes"]["/brand/notices"]["body"]["notices"][0]
        self.assertEqual(boxes[0]["print_line"], boxes[1]["print_line"])
        self.assertLess(boxes[0]["component_lot"], notice["from_lot"])
        self.assertGreaterEqual(boxes[1]["component_lot"], notice["from_lot"])
        self.assertIn(notice["signed"], changes[0]["routes"])
