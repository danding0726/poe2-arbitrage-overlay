import unittest

from poe2arb.ocr_monitor import OcrPollingState, complete_capture


class OcrPollingStateTests(unittest.TestCase):
    def test_start_and_stop_control_polling(self):
        state = OcrPollingState()

        self.assertIsNone(state.begin())
        state.start()
        token = state.begin()
        self.assertIsInstance(token, int)
        self.assertIsNone(state.begin())
        self.assertTrue(state.finish(token))

        state.stop()
        self.assertFalse(state.running)
        self.assertIsNone(state.begin())

    def test_result_from_stopped_or_restarted_session_is_stale(self):
        state = OcrPollingState()
        state.start()
        stopped_token = state.begin()
        state.stop()
        self.assertFalse(state.finish(stopped_token))

        state.start()
        old_target_token = state.begin()
        state.restart()
        self.assertFalse(state.finish(old_target_token))
        self.assertIsNotNone(state.begin())

    def test_only_complete_high_confidence_capture_is_accepted(self):
        panel = {"selected_order": {
            "pay": 30, "receive": 2, "gold": 1000, "confidence": 0.96,
        }}
        ladder = {"stock": 10, "confidence": 0.95, "levels": []}

        accepted = complete_capture((panel, ladder))

        self.assertEqual(accepted[2], (30, 2, 10, 1000))
        self.assertIsNone(complete_capture((panel, {**ladder, "stock": None})))
        self.assertIsNone(complete_capture((panel, {**ladder, "confidence": 0.89})))
        mismatched = {**ladder, "best_quote": {
            "pay": 40, "receive": 2, "confidence": 0.99,
        }}
        self.assertIsNone(complete_capture((panel, mismatched)))


if __name__ == "__main__":
    unittest.main()
