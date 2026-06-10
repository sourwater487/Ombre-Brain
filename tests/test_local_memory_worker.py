import asyncio
import json
import sqlite3
from argparse import Namespace

from scripts import local_memory_worker as worker


def test_read_bridge_messages_filters_chat_and_codex(tmp_path):
    db_path = tmp_path / "haven.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER,
          role TEXT NOT NULL,
          content TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'chat',
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    rows = [
        (1, "user", "keep user", "chat"),
        (1, "assistant", "keep assistant", "codex"),
        (1, "assistant", "drop assistant trace", "trace"),
        (1, "trace", "drop trace", "trace"),
        (2, "user", "other session", "chat"),
    ]
    conn.executemany(
        "INSERT INTO messages(session_id, role, content, source) VALUES(?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()

    messages = worker.read_bridge_messages(db_path, since_id=0, limit=10, session_id=1)

    assert [item["content"] for item in messages] == ["keep user", "keep assistant"]


def test_parse_selector_json_normalizes_memories():
    raw = """
    ```json
    {
      "memories": [
        {
          "content": "  2026-05-27, Xiaoyu chose the Mimo key for local memory worker.  ",
          "tags": ["project_event", "bad_tag", "flavor_test"],
          "importance": 9,
          "reason": "still affects setup",
          "source_message_ids": [10, 11, 999]
        }
      ]
    }
    ```
    """

    memories = worker.parse_selector_json(raw, {10, 11})

    assert memories == [
        {
            "content": "2026-05-27, Xiaoyu chose the Mimo key for local memory worker.",
            "tags": ["project_event", "from_haven_bridge", "auto_memory_worker"],
            "importance": 7,
            "reason": "still affects setup",
            "source_message_ids": [10, 11],
            "hash": worker.memory_hash("2026-05-27, Xiaoyu chose the Mimo key for local memory worker."),
        }
    ]


def test_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = {"last_message_id": 42, "written_hashes": ["abc"]}

    worker.save_state(path, state)

    assert json.loads(path.read_text(encoding="utf-8")) == state
    assert worker.load_state(path) == state


async def _fake_write_memories(memories, state, cfg):
    return []


def test_initial_write_guard_advances_checkpoint(monkeypatch, tmp_path):
    state_file = tmp_path / "worker-state.json"
    args = Namespace(since_id=None, session_id=0, limit=20)
    cfg = worker.WorkerConfig(
        bridge_db=tmp_path / "haven.db",
        state_file=state_file,
        base_url="https://example.test/v1",
        model="mimo-v2.5",
        api_key_env="TEST_KEY",
        timeout_seconds=5,
        max_tokens=100,
        max_items=1,
        duplicate_score=90,
        dry_run=False,
        mark_seen=False,
        allow_initial_write=False,
        settle_seconds=0,
    )
    messages = [
        {
            "id": 7,
            "session_id": 1,
            "role": "user",
            "source": "chat",
            "content": "hello",
            "metadata": {},
            "created_at": "2026-05-27 12:00:00",
        }
    ]
    monkeypatch.setattr(worker, "read_bridge_messages", lambda *args, **kwargs: messages)
    monkeypatch.setattr(worker, "call_selector", lambda *args, **kwargs: '{"memories":[]}')
    monkeypatch.setattr(worker, "write_memories", _fake_write_memories)

    result = asyncio.run(worker.run_once(args, cfg))

    assert result["status"] == "initial_dry_run"
    assert worker.load_state(state_file)["last_message_id"] == 7
