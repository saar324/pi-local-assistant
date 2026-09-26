"""Small, offline chat API for memory-constrained Raspberry Pi computers.

The public repository contains code only. Runtime state is placed in the user's
XDG data directory and the model is served by a loopback-only llama.cpp server.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator


MODEL_NAME = "pi-local-assistant"
CONTEXT_TOKENS = 2048
INPUT_BUDGET = 1400
COMPACT_AT = 1100
MAX_OUTPUT_TOKENS = 256
MAX_REQUEST_BYTES = 65536
SYSTEM_PROMPT = (
    "You are a helpful personal assistant running entirely offline on a small "
    "Raspberry Pi. Answer clearly and briefly. Admit uncertainty. You cannot "
    "browse the web or perform actions unless a separate tool is explicitly "
    "available."
)


class ClientError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class BackendError(Exception):
    pass


def token_estimate(messages: list[dict[str, str]]) -> int:
    """Conservative budget estimate; llama.cpp remains the final context guard."""
    return sum((len(m["content"].encode("utf-8")) + 2) // 3 + 10 for m in messages)


def data_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "pi-local-assistant" / "sessions.sqlite3"


class Store:
    def __init__(self, path: Path):
        self.path = path
        existed = path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if existed and os.stat(path.parent).st_mode & 0o077:
            raise ValueError("The data directory must be private (mode 0700)")
        if not existed:
            os.chmod(path.parent, 0o700)
        if path.exists():
            os.chmod(path, 0o600)
        with self.connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    summary_through INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_session_id_id
                    ON messages(session_id, id);
                """
            )
        os.chmod(path, 0o600)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, title: str = "") -> str:
        session_id = uuid.uuid4().hex
        now = int(time.time())
        with self.connection() as db:
            db.execute(
                "INSERT INTO sessions(id, title, created_at, updated_at) VALUES(?,?,?,?)",
                (session_id, title[:100], now, now),
            )
        return session_id

    def session(self, session_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ClientError("Unknown session", 404)
        return dict(row)

    def pending(self, session_id: str, through: int) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT id, role, content FROM messages "
                "WHERE session_id=? AND id>? ORDER BY id",
                (session_id, through),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_summary(self, session_id: str, summary: str, through: int) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE sessions SET summary=?, summary_through=? WHERE id=?",
                (summary, through, session_id),
            )

    def save_turn(self, session_id: str, user: str, answer: str) -> None:
        now = int(time.time())
        with self.connection() as db:
            db.executemany(
                "INSERT INTO messages(session_id, role, content, created_at) "
                "VALUES(?,?,?,?)",
                [(session_id, "user", user, now), (session_id, "assistant", answer, now)],
            )
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))

    def list_sessions(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT id, title, created_at, updated_at FROM sessions "
                "ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
        return [dict(row) for row in rows]

    def history(self, session_id: str) -> list[dict]:
        self.session(session_id)
        with self.connection() as db:
            rows = db.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]


class Model:
    def __init__(self, base_url: str):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("The model server must use loopback HTTP")
        self.url = base_url.rstrip("/") + "/v1/chat/completions"

    def generate(self, messages: list[dict[str, str]], max_tokens: int = MAX_OUTPUT_TOKENS) -> dict:
        payload = {
            "model": MODEL_NAME,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.6,
            "stream": False,
            "reasoning_effort": "none",
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise BackendError("Local model is unavailable or rejected the request") from exc
        try:
            answer = result["choices"][0]["message"]["content"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("empty model answer")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise BackendError("Local model returned an invalid answer") from exc
        return result


class Assistant:
    def __init__(self, store: Store, model: Model):
        self.store = store
        self.model = model
        self.lock = threading.Lock()

    @staticmethod
    def context(summary: str, rows: list[dict], user: str) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if summary:
            messages.append({"role": "system", "content": "Earlier conversation summary: " + summary})
        messages.extend({"role": row["role"], "content": row["content"]} for row in rows)
        messages.append({"role": "user", "content": user})
        return messages

    def compact(self, session_id: str, user: str) -> list[dict[str, str]]:
        while True:
            session = self.store.session(session_id)
            rows = self.store.pending(session_id, session["summary_through"])
            context = self.context(session["summary"], rows, user)
            if token_estimate(context) <= COMPACT_AT:
                return context
            if not rows:
                raise ClientError("Message is too large for this model's context", 413)

            # Compact old messages in bounded batches. Preserve the latest turn.
            candidates = rows[:-2] if len(rows) > 2 else rows
            batch: list[dict] = []
            batch_prompt: list[dict[str, str]] | None = None
            for count in range(2, len(candidates) + 1, 2):
                trial = candidates[:count]
                transcript = "\n".join(f"{item['role']}: {item['content']}" for item in trial)
                summary_prompt = [
                    {"role": "system", "content": "Summarize durable facts, decisions, and open questions in under 600 characters. Do not invent facts."},
                    {"role": "user", "content": "Previous summary:\n" + session["summary"] + "\nOlder messages:\n" + transcript},
                ]
                if token_estimate(summary_prompt) > INPUT_BUDGET:
                    break
                batch = trial
                batch_prompt = summary_prompt
            if not batch or batch_prompt is None:
                raise ClientError("An older message is too large to compact", 413)
            result = self.model.generate(batch_prompt, max_tokens=160)
            summary = result["choices"][0]["message"]["content"].strip()[:800]
            if not summary:
                raise BackendError("Local model could not summarize the conversation")
            self.store.save_summary(session_id, summary, batch[-1]["id"])

    def chat(self, session_id: str, user: str) -> str:
        if not isinstance(user, str) or not user.strip():
            raise ClientError("A non-empty user message is required")
        if token_estimate([{"content": user}]) > 700:
            raise ClientError("Message is too large for this model; send a shorter one", 413)
        self.store.session(session_id)
        if not self.lock.acquire(blocking=False):
            raise ClientError("The Pi is answering another request; retry shortly", 429)
        try:
            context = self.compact(session_id, user)
            if token_estimate(context) > INPUT_BUDGET:
                raise ClientError("Conversation exceeds the model's context", 413)
            result = self.model.generate(context)
            answer = result["choices"][0]["message"]["content"].strip()
            self.store.save_turn(session_id, user, answer)
            return answer
        finally:
            self.lock.release()

    def complete(self, messages: list[dict], max_tokens: int) -> dict:
        if not isinstance(messages, list) or not messages:
            raise ClientError("messages must be a non-empty array")
        clean = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
                raise ClientError("Only system, user, and assistant text messages are supported")
            content = message.get("content")
            if not isinstance(content, str):
                raise ClientError("Message content must be text")
            clean.append({"role": message["role"], "content": content})
        if token_estimate(clean) > INPUT_BUDGET:
            raise ClientError("Request exceeds the model's context; use a durable session", 413)
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= MAX_OUTPUT_TOKENS:
            raise ClientError(f"max_tokens must be between 1 and {MAX_OUTPUT_TOKENS}")
        if not self.lock.acquire(blocking=False):
            raise ClientError("The Pi is answering another request; retry shortly", 429)
        try:
            return self.model.generate(clean, max_tokens)
        finally:
            self.lock.release()


class ApiHandler(BaseHTTPRequestHandler):
    assistant: Assistant
    api_key: str

    def log_message(self, format: str, *args: object) -> None:
        # Never write prompts, responses, or bearer tokens to an access log.
        pass

    def send_json(self, status: int, value: dict | list) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorize(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = "Bearer " + self.api_key
        if hmac.compare_digest(header, expected):
            return True
        self.send_json(401, {"error": "Unauthorized"})
        return False

    def read_json(self) -> dict:
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError as exc:
            raise ClientError("Invalid Content-Length") from exc
        if size < 0 or size > MAX_REQUEST_BYTES:
            raise ClientError("Request body is too large or missing", 413)
        try:
            body = json.loads(self.rfile.read(size))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClientError("Invalid JSON") from exc
        if not isinstance(body, dict):
            raise ClientError("Expected a JSON object")
        return body

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"status": "ok"})
            return
        if not self.authorize():
            return
        try:
            if self.path == "/v1/models":
                self.send_json(200, {"object": "list", "data": [{"id": MODEL_NAME, "object": "model", "context_length": CONTEXT_TOKENS}]})
            elif self.path == "/api/sessions":
                self.send_json(200, {"sessions": self.assistant.store.list_sessions()})
            elif self.path.startswith("/api/sessions/") and self.path.endswith("/messages"):
                session_id = self.path.split("/")[3]
                self.send_json(200, {"messages": self.assistant.store.history(session_id)})
            else:
                self.send_json(404, {"error": "Not found"})
        except ClientError as exc:
            self.send_json(exc.status, {"error": str(exc)})

    def do_POST(self) -> None:
        if not self.authorize():
            return
        try:
            body = self.read_json()
            if self.path == "/api/sessions":
                title = body.get("title", "")
                if not isinstance(title, str):
                    raise ClientError("title must be text")
                session_id = self.assistant.store.create(title)
                self.send_json(201, {"session_id": session_id})
            elif self.path.startswith("/api/sessions/") and self.path.endswith("/chat"):
                session_id = self.path.split("/")[3]
                answer = self.assistant.chat(session_id, body.get("input"))
                self.send_json(200, {"session_id": session_id, "output": answer})
            elif self.path == "/v1/chat/completions":
                if body.get("stream", False):
                    raise ClientError("Streaming is not supported in this version")
                session_id = body.get("session_id")
                if session_id is not None:
                    messages = body.get("messages")
                    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict) or messages[-1].get("role") != "user":
                        raise ClientError("A session request needs a final user message")
                    answer = self.assistant.chat(session_id, messages[-1].get("content"))
                    result = {
                        "id": "chatcmpl-" + uuid.uuid4().hex,
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": MODEL_NAME,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                        "session_id": session_id,
                    }
                else:
                    result = self.assistant.complete(body.get("messages"), body.get("max_tokens", MAX_OUTPUT_TOKENS))
                self.send_json(200, result)
            else:
                self.send_json(404, {"error": "Not found"})
        except ClientError as exc:
            self.send_json(exc.status, {"error": str(exc)})
        except BackendError as exc:
            self.send_json(502, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline assistant for small Raspberry Pi devices")
    parser.add_argument("--model-url", default="http://127.0.0.1:8080")
    parser.add_argument("--data-path", type=Path, default=data_path())
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="start the personal OpenAI-style API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    chat = sub.add_parser("chat", help="chat in the terminal")
    chat.add_argument("--session-id")
    sub.add_parser("sessions", help="list saved conversations")
    args = parser.parse_args()
    os.umask(0o077)
    assistant = Assistant(Store(args.data_path), Model(args.model_url))

    if args.command == "sessions":
        for session in assistant.store.list_sessions():
            print(session["id"], session["title"])
        return
    if args.command == "serve":
        key = os.environ.get("PI_ASSISTANT_API_KEY", "")
        if len(key) < 24:
            parser.error("set PI_ASSISTANT_API_KEY to a private random value of at least 24 characters")
        handler = type("BoundHandler", (ApiHandler,), {"assistant": assistant, "api_key": key})
        server = ThreadingHTTPServer((args.host, args.port), handler)
        server.daemon_threads = True
        print(f"Pi assistant API listening on {args.host}:{args.port}", flush=True)
        server.serve_forever()
    else:
        session_id = args.session_id or assistant.store.create()
        assistant.store.session(session_id)
        print(f"Session: {session_id}\nType /quit to exit.")
        while True:
            try:
                prompt = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if prompt == "/quit":
                break
            if not prompt:
                continue
            try:
                print("Assistant:", assistant.chat(session_id, prompt), "\n")
            except (ClientError, BackendError) as exc:
                print("Error:", exc)


if __name__ == "__main__":
    main()
