#!/usr/bin/env python3
"""Rebuild the optional hnswlib vector index from embeddings.db."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from embedding_engine import VectorIndexHNSW
from utils import load_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None, help="Path to config YAML. Defaults to OMBRE_CONFIG_PATH/config.yaml.")
    parser.add_argument("--db-path", default="", help="Override embeddings.db path.")
    parser.add_argument("--index-path", default="", help="Override output HNSW index path.")
    parser.add_argument("--labels-path", default="", help="Override output labels JSON path.")
    args = parser.parse_args()

    config = load_config(args.config)
    embedding_cfg = config.setdefault("embedding", {})
    vector_cfg = embedding_cfg.setdefault("vector_index", {})
    vector_cfg["enabled"] = True
    vector_cfg.setdefault("backend", "hnswlib")
    if args.index_path:
        vector_cfg["index_path"] = args.index_path
    if args.labels_path:
        vector_cfg["labels_path"] = args.labels_path

    db_path = args.db_path or os.path.join(config["buckets_dir"], "embeddings.db")
    index = VectorIndexHNSW(
        config,
        db_path,
        str(embedding_cfg.get("model") or "gemini-embedding-001"),
        force_enabled=True,
    )
    stats = index.rebuild_from_sqlite()
    print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
