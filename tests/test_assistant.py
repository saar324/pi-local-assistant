import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from pi_assistant import ApiHandler, Assistant, ClientError, Store, api_key, token_estimate, INPUT_BUDGET


class FakeModel:
    def __init__(self):
        self.calls = []

    def generate(self, messages, max_tokens=256):
        self.calls.append(messages)
        answer = "A compact summary of earlier decisions." if max_tokens == 160 else "A local answer."
        return {
            "id": "fake-1",
            "object": "chat.completion",
            "model": "pi-local-assistant",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        }


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "private" / "sessions.sqlite3")
        self.model = FakeModel()
        self.assistant = Assistant(self.store, self.model)

    def tearDown(self):
        self.temp.cleanup()

    def test_compaction_preserves_full_history(self):
        session_id = self.store.create("test")
        for index in range(12):
            self.store.save_turn(session_id, f"Fact {index}: " + "A" * 190, "Noted.")
        output = self.assistant.chat(session_id, "What have we decided?")
        self.assertEqual(output, "A local answer.")
        self.assertEqual(len(self.store.history(session_id)), 26)
        self.assertGreater(self.store.session(session_id)["summary_through"], 0)
        self.assertTrue(any(call[0]["content"].startswith("Summarize") for call in self.model.calls))
        self.assertLessEqual(token_estimate(self.model.calls[-1]), INPUT_BUDGET)

    def test_private_store_and_authenticated_api(self):
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        handler = type("TestHandler", (ApiHandler,), {"assistant": self.assistant, "api_key": "a" * 24})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/") as response:
                self.assertIn("Pi Local Assistant", response.read().decode())
                self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
            with urllib.request.urlopen(base + "/app.js") as response:
                self.assertIn("backend_request", response.read().decode())
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + "/v1/models")
            self.assertEqual(error.exception.code, 401)
            request = urllib.request.Request(
                base + "/api/sessions",
                data=json.dumps({"title": "private"}).encode(),
                headers={"Authorization": "Bearer " + "a" * 24, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                session_id = json.load(response)["session_id"]
            self.assertEqual(self.store.session(session_id)["title"], "private")
            chat_request = urllib.request.Request(
                base + f"/api/sessions/{session_id}/chat",
                data=json.dumps({"input": "Hello from the API"}).encode(),
                headers={"Authorization": "Bearer " + "a" * 24, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(chat_request) as response:
                self.assertEqual(json.load(response)["output"], "A local answer.")
            self.assertEqual(len(self.store.history(session_id)), 2)
            inspected = urllib.request.Request(
                base + "/v1/chat/completions",
                data=json.dumps({"model": "pi-local-assistant", "session_id": session_id, "inspect": True,
                                 "messages": [{"role": "user", "content": "Explain briefly"}]}).encode(),
                headers={"Authorization": "Bearer " + "a" * 24, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(inspected) as response:
                result = json.load(response)
            self.assertEqual(result["session_id"], session_id)
            self.assertEqual(result["choices"][0]["message"]["content"], "A local answer.")
            model_request = result["inspection"]["backend_request"]
            self.assertEqual(model_request["reasoning_effort"], "none")
            self.assertEqual(model_request["messages"][-1]["content"], "Explain briefly")
            self.assertEqual(len(self.store.history(session_id)), 4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_rejects_shared_data_directory_without_changing_it(self):
        shared = Path(self.temp.name) / "shared"
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        with self.assertRaises(ValueError):
            Store(shared / "sessions.sqlite3")
        self.assertEqual(os.stat(shared).st_mode & 0o777, 0o755)

    def test_history_is_paginated_to_bound_memory(self):
        session_id = self.store.create()
        for _ in range(60):
            self.store.save_turn(session_id, "question", "answer")
        page = self.store.history(session_id)
        self.assertEqual(len(page), 100)
        older = self.store.history(session_id, before_id=page[0]["id"])
        self.assertEqual(len(older), 20)
        self.assertLess(older[-1]["id"], page[0]["id"])

    def test_invalid_session_id_is_rejected(self):
        with self.assertRaisesRegex(ClientError, "Invalid session ID"):
            self.store.session({"not": "an ID"})

    def test_systemd_credential_stays_out_of_configuration(self):
        credentials_dir = Path(self.temp.name) / "credentials"
        credentials_dir.mkdir()
        (credentials_dir / "api_key").write_text("private-test-key\n")
        with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(credentials_dir)}, clear=True):
            self.assertEqual(api_key(), "private-test-key")


if __name__ == "__main__":
    unittest.main()
