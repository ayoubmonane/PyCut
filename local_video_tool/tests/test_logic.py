import unittest

from processor import format_timestamp, make_timestamps, parse_frame_rate


class ProcessorLogicTests(unittest.TestCase):
    def test_timestamps_start_at_zero_and_do_not_exceed_duration(self):
        self.assertEqual(make_timestamps(25.5), [0, 10, 20])

    def test_exact_ten_second_duration_does_not_seek_past_last_frame(self):
        self.assertEqual(make_timestamps(10.0), [0])

    def test_zero_duration(self):
        self.assertEqual(make_timestamps(0), [0])

    def test_fractional_frame_rate(self):
        self.assertAlmostEqual(parse_frame_rate("30000/1001"), 29.97002997, places=6)

    def test_invalid_frame_rate(self):
        self.assertIsNone(parse_frame_rate("0/0"))
        self.assertIsNone(parse_frame_rate("N/A"))

    def test_timestamp_format(self):
        self.assertEqual(format_timestamp(65.25), "00:01:05.250")


if __name__ == "__main__":
    unittest.main()
