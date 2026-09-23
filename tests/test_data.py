import unittest
from unittest.mock import patch

from poe2arb.data import HistoryPageUnavailable, sync_recent


class DataTests(unittest.TestCase):
    def test_end_of_stream_404_keeps_successful_pages(self):
        now_hour = 1_800_000_000
        first_id = now_hour - 6 * 3600
        market = {"league": "Test", "market_id": "pair", "market_pair": ["a", "b"]}

        with (
            patch("poe2arb.data.time.time", return_value=now_hour),
            patch("poe2arb.data.load_snapshot", return_value={"markets": [], "last_id": None}),
            patch(
                "poe2arb.data.fetch_page",
                side_effect=[
                    {"markets": [market], "next_change_id": first_id + 3600},
                    HistoryPageUnavailable("not published"),
                ],
            ),
            patch("poe2arb.data.save_snapshot") as save,
        ):
            snapshot = sync_recent()

        self.assertEqual(len(snapshot["markets"]), 1)
        self.assertEqual(snapshot["markets"][0]["_hour_id"], first_id)
        self.assertEqual(snapshot["last_id"], first_id)
        save.assert_called_once_with(snapshot)

    def test_unavailable_cached_cursor_retries_recent_window(self):
        now_hour = 1_800_000_000
        cached_id = now_hour - 3600
        recent_id = now_hour - 6 * 3600

        with (
            patch("poe2arb.data.time.time", return_value=now_hour),
            patch("poe2arb.data.load_snapshot", return_value={"markets": [], "last_id": cached_id}),
            patch(
                "poe2arb.data.fetch_page",
                side_effect=[
                    HistoryPageUnavailable("bad cursor"),
                    {"markets": [], "next_change_id": recent_id},
                ],
            ) as fetch,
            patch("poe2arb.data.save_snapshot"),
        ):
            snapshot = sync_recent()

        self.assertEqual([call.args[0] for call in fetch.call_args_list], [cached_id, recent_id])
        self.assertEqual(snapshot["last_id"], recent_id)

    def test_empty_successful_page_still_makes_terminal_404_normal(self):
        now_hour = 1_800_000_000
        first_id = now_hour - 6 * 3600

        with (
            patch("poe2arb.data.time.time", return_value=now_hour),
            patch("poe2arb.data.load_snapshot", return_value={"markets": [], "last_id": None}),
            patch(
                "poe2arb.data.fetch_page",
                side_effect=[
                    {"markets": [], "next_change_id": first_id + 3600},
                    HistoryPageUnavailable("not published"),
                ],
            ),
            patch("poe2arb.data.save_snapshot"),
        ):
            snapshot = sync_recent()

        self.assertEqual(snapshot["markets"], [])
        self.assertEqual(snapshot["last_id"], first_id)


if __name__ == "__main__":
    unittest.main()
