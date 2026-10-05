import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("bridge.py")
SPEC = importlib.util.spec_from_file_location("bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def test_markdown_links_are_safe_html(self):
        self.assertEqual(
            bridge.markup_to_html("Сайт [пример](https://example.test/a?x=1&y=2)", []),
            'Сайт <a href="https://example.test/a?x=1&amp;y=2">пример</a>',
        )
        self.assertEqual(
            bridge.markup_to_html("[опасно](javascript:alert(1)) и ` [код](https://example.test) `", []),
            "[опасно](javascript:alert(1)) и ` [код](https://example.test) `",
        )
        self.assertEqual(bridge.markup_to_html("<тег> & текст", []), "&lt;тег&gt; &amp; текст")

    def test_reaction_reports_total_failure(self):
        cfg = {"max_chat_ids": [1, 2]}
        state = {"feed": [{"id": 10, "chat_id": 99, "name": "Иван", "text": "Тест"}]}
        with patch.object(bridge, "max_send", side_effect=RuntimeError("offline")):
            answer = bridge.apply_secretary_data(None, cfg, state, json.dumps({"react": {"id": 10, "emoji": "👍"}}), 5, "Анна")
        self.assertIn("Не удалось", answer)

    def test_reaction_reports_partial_success(self):
        cfg = {"max_chat_ids": [1, 2]}
        state = {"feed": [{"id": 10, "chat_id": 99, "name": "Иван", "text": "Тест"}]}
        with patch.object(bridge, "max_send", side_effect=[None, RuntimeError("offline")]):
            answer = bridge.apply_secretary_data(None, cfg, state, json.dumps({"react": {"id": 10, "emoji": "👍"}}), 5, "Анна")
        self.assertIn("1 из 2", answer)

    def test_first_private_message_is_relayed(self):
        cfg = {"tg_token": "token", "max_chat_ids": [1], "tg_admin_ids": "999"}
        state = {"tg_welcomed": []}
        updates = [{"update_id": 1, "message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7, "first_name": "Анна"}, "text": "Привет"}}]
        with patch.object(bridge, "tg_send", return_value=updates), patch.object(bridge, "send_secretary_keyboard") as keyboard, patch.object(bridge, "relay_tg_to_max") as relay, patch.object(bridge, "save_state"):
            bridge.poll_tg_admin(None, cfg, state)
        keyboard.assert_called_once()
        relay.assert_called_once()

    def test_processed_updates_are_bounded(self):
        state = {"processed_max_updates": []}
        for number in range(bridge.PROCESSED_UPDATES_LIMIT + 5):
            bridge.remember_processed_update(state, str(number))
        self.assertEqual(len(state["processed_max_updates"]), bridge.PROCESSED_UPDATES_LIMIT)
        self.assertEqual(state["processed_max_updates"][0], "5")

    def test_init_persists_discovered_max_chat(self):
        cfg = {"max_token": "max", "tg_token": "tg", "max_chat_ids": [], "max_chat_id": None, "tg_chat_id": 42}
        state = {}
        response = type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"updates": [{"update_type": "bot_added", "chat_id": 123}]}})()
        session = type("Session", (), {"get": lambda self, *args, **kwargs: response})()
        with patch.object(bridge, "load_state", return_value=state), patch.object(bridge, "save_state"), patch.object(bridge, "max_session", return_value=session), patch.object(bridge, "time") as clock:
            clock.time.side_effect = [0, 0, 0]
            bridge.run_init(cfg)
        self.assertEqual(cfg["max_chat_ids"], [123])
        self.assertEqual(state["max_chat_ids"], [123])

    def test_tokens_must_come_from_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, "CONFIG_PATH", os.path.join(directory, "config.json")), patch.object(bridge, "STATE_PATH", os.path.join(directory, "state.json")), patch.dict(os.environ, {"MAX_TOKEN": "max", "TG_TOKEN": "tg"}, clear=True):
            Path(bridge.CONFIG_PATH).write_text(json.dumps({"max_token": "old", "tg_token": "old"}), encoding="utf-8")
            cfg = bridge.load_config()
        self.assertEqual(cfg["max_token"], "max")
        self.assertEqual(cfg["tg_token"], "tg")

    def test_truncate_respects_utf16_limits(self):
        emoji = "😀" * 3000
        truncated = bridge.truncate(emoji, bridge.TG_TEXT_LIMIT)
        self.assertLessEqual(bridge.utf16_len(truncated), bridge.TG_TEXT_LIMIT)
        self.assertEqual(bridge.truncate("привет", 4096), "привет")
        self.assertTrue(bridge.truncate("a" * 5000, 4096).endswith("…"))

    def test_failed_update_counter_increments(self):
        state = {"processed_max_updates": [], "failed_max_updates": {}}
        self.assertEqual(bridge.count_failed_update(state, "123:456"), 1)
        self.assertEqual(bridge.count_failed_update(state, "123:456"), 2)
        self.assertEqual(bridge.count_failed_update(state, "123:456"), 3)

    def test_poisonous_update_is_skipped_after_max_attempts(self):
        cfg = {"tg_token": "t", "tg_chat_id": 10}
        state = {"marker": 5, "processed_max_updates": [], "failed_max_updates": {}, "rules": {}, "recent": [], "feed": []}
        key = "1:456"
        alerts = []
        processed = set(state["processed_max_updates"])

        def handle_fail(*args, **kwargs):
            raise RuntimeError("poison")

        for attempt in range(bridge.MAX_SEND_ATTEMPTS):
            failed = bridge.count_failed_update(state, key)
            self.assertEqual(failed, attempt + 1)
            if failed < bridge.MAX_SEND_ATTEMPTS:
                self.assertIn(key, state["failed_max_updates"])
        with patch.object(bridge, "tg_send", side_effect=lambda *a, **k: alerts.append(k) or {}):
            failed = bridge.count_failed_update(state, key)
            self.assertGreaterEqual(failed, bridge.MAX_SEND_ATTEMPTS)
            remember = bridge.remember_processed_update(state, key)
            processed.add(key)
        self.assertIn(key, state["processed_max_updates"])


if __name__ == "__main__":
    unittest.main()
