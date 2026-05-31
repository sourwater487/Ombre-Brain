#!/usr/bin/env python3
"""Find orphan embedding rows and optionally delete them."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine
from utils import load_config


@dataclass(frozen=True)
class EmbeddingRow:
    bucket_id: str
    updated_at: str | None


def _clean_bucket_id(value) -> str | None:
    if not isinstance(value, str):
        return None
    bucket_id = value.strip()
    return bucket_id or None


def embedding_rows(db_path: str | Path) -> list[EmbeddingRow]:
    """Read stored embedding bucket ids. Missing db/table is treated as empty."""
    path = Path(db_path)
    if not path.exists():
        return []

    try:
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute("SELECT bucket_id, updated_at FROM embeddings").fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return []

    result: list[EmbeddingRow] = []
    for bucket_id, updated_at in rows:
        clean_id = _clean_bucket_id(bucket_id)
        if not clean_id:
            continue
        result.append(EmbeddingRow(bucket_id=clean_id, updated_at=updated_at))
    return result


def find_orphan_embeddings(
    rows: list[EmbeddingRow],
    live_bucket_ids: set[str],
) -> list[EmbeddingRow]:
    live_ids = {_clean_bucket_id(bucket_id) for bucket_id in live_bucket_ids}
    live_ids.discard(None)
    return [row for row in rows if row.bucket_id not in live_ids]


def delete_embeddings(db_path: str | Path, bucket_ids: list[str]) -> int:
    clean_ids = sorted(
        bucket_id
        for bucket_id in {_clean_bucket_id(bucket_id) for bucket_id in bucket_ids}
        if bucket_id
    )
    if not clean_ids:
        return 0

    path = Path(db_path)
    if not path.exists():
        return 0

    placeholders = ",".join("?" for _ in clean_ids)
    try:
        conn = sqlite3.connect(path)
        try:
            cursor = conn.execute(
                f"DELETE FROM embeddings WHERE bucket_id IN ({placeholders})",
                clean_ids,
            )
            conn.commit()
            return max(0, cursor.rowcount)
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0


def live_bucket_ids(buckets: list[dict]) -> set[str]:
    ids: set[str] = set()
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        bucket_id = _clean_bucket_id(bucket.get("id"))
        if bucket_id:
            ids.add(bucket_id)
    return ids


def print_summary(rows: list[EmbeddingRow], orphans: list[EmbeddingRow], limit: int) -> None:
    print(f"Embeddings: {len(rows)} / Orphan embeddings: {len(orphans)}")

    if not orphans:
        return

    show_count = min(max(0, limit), len(orphans))
    print(f"Orphan embedding ids (showing {show_count} of {len(orphans)}):")
    for row in orphans[:show_count]:
        suffix = f" updated_at={row.updated_at}" if row.updated_at else ""
        print(f"  {row.bucket_id}{suffix}")
    if len(orphans) > show_count:
        print(f"  ... and {len(orphans) - show_count} more")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true", help="Delete orphan rows from embeddings.db.")
    parser.add_argument("--yes", action="store_true", help="Skip the DELETE confirmation prompt.")
    parser.add_argument("--limit", type=int, default=20, help="How many orphan ids to print.")
    args = parser.parse_args()

    config = load_config()
    bucket_mgr = BucketManager(config)
    embedding_engine = EmbeddingEngine(config)

    buckets = await bucket_mgr.list_all(include_archive=True)
    live_ids = live_bucket_ids(buckets)
    rows = embedding_rows(embedding_engine.db_path)
    orphans = find_orphan_embeddings(rows, live_ids)

    print(f"Live buckets: {len(live_ids)}")
    print_summary(rows, orphans, args.limit)

    if not args.delete:
        return 0

    if not orphans:
        print("Deleted embeddings: 0")
        return 0

    if not args.yes:
        answer = input("Type DELETE to delete orphan embedding rows: ")
        if answer != "DELETE":
            print("Canceled.")
            return 0

    deleted = delete_embeddings(embedding_engine.db_path, [row.bucket_id for row in orphans])
    print(f"Deleted embeddings: {deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
