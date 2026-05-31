import importlib.util
import sqlite3
import sys
from pathlib import Path


def _load_cleanup_module():
    path = Path("scripts/cleanup_orphan_embeddings.py")
    spec = importlib.util.spec_from_file_location("cleanup_orphan_embeddings", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _create_embeddings_db(path: Path, rows=None):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE embeddings (
            bucket_id TEXT PRIMARY KEY,
            embedding TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    for bucket_id, updated_at in rows or []:
        conn.execute(
            "INSERT INTO embeddings (bucket_id, embedding, updated_at) VALUES (?, ?, ?)",
            (bucket_id, "[0.1, 0.2]", updated_at),
        )
    conn.commit()
    conn.close()


def test_embedding_rows_returns_empty_when_db_does_not_exist(tmp_path):
    cleanup = _load_cleanup_module()
    db_path = tmp_path / "missing.db"

    assert cleanup.embedding_rows(db_path) == []
    assert not db_path.exists()


def test_embedding_rows_returns_empty_when_embeddings_table_does_not_exist(tmp_path):
    cleanup = _load_cleanup_module()
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE notes (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    assert cleanup.embedding_rows(db_path) == []


def test_find_orphan_embeddings_finds_rows_not_in_live_ids():
    cleanup = _load_cleanup_module()
    rows = [
        cleanup.EmbeddingRow("alpha", "2026-01-01T00:00:00Z"),
        cleanup.EmbeddingRow("beta", "2026-01-02T00:00:00Z"),
        cleanup.EmbeddingRow("gamma", "2026-01-03T00:00:00Z"),
    ]

    orphans = cleanup.find_orphan_embeddings(rows, {"alpha", "gamma"})

    assert [row.bucket_id for row in orphans] == ["beta"]


def test_delete_embeddings_deletes_selected_bucket_ids(tmp_path):
    cleanup = _load_cleanup_module()
    db_path = tmp_path / "embeddings.db"
    _create_embeddings_db(
        db_path,
        [
            ("alpha", "2026-01-01T00:00:00Z"),
            ("beta", "2026-01-02T00:00:00Z"),
            ("gamma", "2026-01-03T00:00:00Z"),
        ],
    )

    deleted = cleanup.delete_embeddings(db_path, ["beta", "gamma"])
    remaining = [row.bucket_id for row in cleanup.embedding_rows(db_path)]

    assert deleted == 2
    assert remaining == ["alpha"]


def test_delete_embeddings_empty_list_returns_zero(tmp_path):
    cleanup = _load_cleanup_module()
    db_path = tmp_path / "embeddings.db"
    _create_embeddings_db(db_path, [("alpha", "2026-01-01T00:00:00Z")])

    assert cleanup.delete_embeddings(db_path, []) == 0
    assert [row.bucket_id for row in cleanup.embedding_rows(db_path)] == ["alpha"]


def test_archived_bucket_id_in_live_ids_is_not_orphan():
    cleanup = _load_cleanup_module()
    rows = [
        cleanup.EmbeddingRow("active", "2026-01-01T00:00:00Z"),
        cleanup.EmbeddingRow("archived", "2026-01-02T00:00:00Z"),
        cleanup.EmbeddingRow("removed", "2026-01-03T00:00:00Z"),
    ]

    orphans = cleanup.find_orphan_embeddings(rows, {"active", "archived"})

    assert [row.bucket_id for row in orphans] == ["removed"]
