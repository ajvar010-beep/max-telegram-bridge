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

    def test_tokens_must_come_from_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, "CONFIG_PATH", os.path.join(directory, "config.json")), patch.object(bridge, "STATE_PATH", os.path.join(directory, "state.json")), patch.dict(os.environ, {"MAX_TOKEN": "max", "TG_TOKEN": "tg"}, clear=True):
            Path(bridge.CONFIG_PATH).write_text(json.dumps({"max_token": "old", "tg_token": "old"}), encoding="utf-8")
            cfg = bridge.load_config()
        self.assertEqual(cfg["max_token"], "max")
        self.assertEqual(cfg["tg_token"], "tg")


if __name__ == "__main__":
    unittest.main()
