# ============================================================
# Module: Embedding Engine (embedding_engine.py)
# 模块：向量化引擎
#
# Generates embeddings via Gemini API (OpenAI-compatible),
# stores them in SQLite, and provides cosine similarity search.
# 通过 Gemini API（OpenAI 兼容）生成 embedding，
# 存储在 SQLite 中，提供余弦相似度搜索。
#
# Depended on by: server.py, bucket_manager.py
# 被谁依赖：server.py, bucket_manager.py
# ============================================================

import os
import json
import math
import sqlite3
import logging
import asyncio
import time

logger = logging.getLogger("ombre_brain.embedding")


def _bool_value(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class VectorIndexHNSW:
    """Optional hnswlib cache over embeddings.db; SQLite remains authoritative."""

    def __init__(self, config: dict, db_path: str, model: str, *, force_enabled: bool = False):
        config = config or {}
        embed_cfg = config.get("embedding", {}) or {}
        index_cfg = embed_cfg.get("vector_index", {}) or {}
        self.db_path = db_path
        self.model = model
        self.backend = str(index_cfg.get("backend") or "hnswlib").strip().lower()
        self.space = str(index_cfg.get("space") or "cosine").strip().lower()
        self.M = EmbeddingEngine._int_between(index_cfg.get("M", 16), 16, 2, 128)
        self.ef_construction = EmbeddingEngine._int_between(index_cfg.get("ef_construction", 100), 100, 10, 1000)
        self.ef_search = EmbeddingEngine._int_between(index_cfg.get("ef_search", 40), 40, 1, 1000)
        state_dir = config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(config.get("buckets_dir") or os.path.dirname(db_path))),
            "state",
        )
        self.index_path = str(index_cfg.get("index_path") or os.path.join(state_dir, "vector_hnsw.index"))
        self.labels_path = str(index_cfg.get("labels_path") or os.path.join(state_dir, "vector_hnsw_labels.json"))
        self.enabled = (force_enabled or _bool_value(index_cfg.get("enabled"), False)) and self.backend == "hnswlib"
        self._hnswlib = None
        self._numpy = None
        self._index = None
        self._label_map: dict[int, str] = {}
        self._dimension: int | None = None
        self._load_failed = False

        if self.enabled:
            try:
                import hnswlib  # type: ignore
                import numpy  # type: ignore

                self._hnswlib = hnswlib
                self._numpy = numpy
            except Exception as exc:
                self.enabled = False
                logger.warning("Vector index disabled: hnswlib import failed | error_type=%s", type(exc).__name__)

            if self.enabled and _bool_value(index_cfg.get("rebuild_on_start"), False):
                try:
                    self.rebuild_from_sqlite()
                except Exception as exc:
                    logger.warning("Vector index rebuild_on_start failed | error_type=%s", type(exc).__name__)

    def rebuild_from_sqlite(self) -> dict:
        if not self.enabled or self._hnswlib is None or self._numpy is None:
            raise RuntimeError("hnswlib vector index is not enabled")

        rows = self._read_sqlite_vectors()
        vectors = []
        bucket_ids = []
        dimension = 0
        for bucket_id, embedding in rows:
            if not dimension:
                dimension = len(embedding)
            if len(embedding) != dimension:
                continue
            bucket_ids.append(bucket_id)
            vectors.append(embedding)

        os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.labels_path), exist_ok=True)

        if not vectors:
            self._write_labels({"dimension": 0, "model": self.model, "labels": {}})
            return {
                "rows_count": 0,
                "dimension": 0,
                "index_path": self.index_path,
                "labels_path": self.labels_path,
            }

        labels = list(range(len(vectors)))
        data = self._numpy.asarray(vectors, dtype=self._numpy.float32)
        index = self._hnswlib.Index(space=self.space, dim=dimension)
        index.init_index(max_elements=len(vectors), ef_construction=self.ef_construction, M=self.M)
        index.add_items(data, self._numpy.asarray(labels, dtype=self._numpy.int64))
        index.set_ef(min(max(self.ef_search, 1), len(vectors)))
        index.save_index(self.index_path)
        self._write_labels(
            {
                "dimension": dimension,
                "model": self.model,
                "space": self.space,
                "labels": {str(label): bucket_id for label, bucket_id in zip(labels, bucket_ids)},
            }
        )
        self._index = index
        self._label_map = {label: bucket_id for label, bucket_id in zip(labels, bucket_ids)}
        self._dimension = dimension
        self._load_failed = False
        return {
            "rows_count": len(vectors),
            "dimension": dimension,
            "index_path": self.index_path,
            "labels_path": self.labels_path,
        }

    def search(self, query_vector: list[float], k: int) -> list[tuple[str, float]] | None:
        if not self.enabled or not query_vector or k <= 0:
            return None
        if not self._ensure_loaded(len(query_vector)):
            return None
        if not self._index or not self._label_map or self._numpy is None:
            return None
        try:
            query = self._numpy.asarray([query_vector], dtype=self._numpy.float32)
            limit = min(k, len(self._label_map))
            labels, distances = self._index.knn_query(query, k=limit)
        except Exception as exc:
            logger.warning("Vector index search failed, falling back to sqlite cosine | error_type=%s", type(exc).__name__)
            return None

        results: list[tuple[str, float]] = []
        for label, distance in zip(labels[0], distances[0]):
            bucket_id = self._label_map.get(int(label))
            if not bucket_id:
                continue
            results.append((bucket_id, self._distance_to_score(float(distance))))
        return results

    def _ensure_loaded(self, query_dimension: int) -> bool:
        if self._load_failed:
            return False
        if self._index is not None and self._dimension == query_dimension:
            return True
        if not os.path.exists(self.index_path) or not os.path.exists(self.labels_path):
            return False
        try:
            labels_doc = self._read_labels()
            dimension = int(labels_doc.get("dimension") or 0)
            if str(labels_doc.get("model") or "") != self.model:
                logger.warning("Vector index model mismatch, falling back to sqlite cosine")
                return False
            if dimension != query_dimension:
                logger.warning(
                    "Vector index dimension mismatch, falling back to sqlite cosine | index_dimension=%s query_dimension=%s",
                    dimension,
                    query_dimension,
                )
                return False
            raw_labels = labels_doc.get("labels")
            if not isinstance(raw_labels, dict):
                raise ValueError("labels must be a dict")
            label_map = {int(label): str(bucket_id) for label, bucket_id in raw_labels.items() if str(bucket_id)}
            if not label_map:
                return False
            index = self._hnswlib.Index(space=self.space, dim=dimension)
            index.load_index(self.index_path)
            index.set_ef(min(max(self.ef_search, 1), len(label_map)))
            self._index = index
            self._label_map = label_map
            self._dimension = dimension
            return True
        except Exception as exc:
            self._index = None
            self._label_map = {}
            self._dimension = None
            self._load_failed = True
            logger.warning("Vector index load failed, falling back to sqlite cosine | error_type=%s", type(exc).__name__)
            return False

    def _read_sqlite_vectors(self) -> list[tuple[str, list[float]]]:
        if not os.path.exists(self.db_path):
            return []
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT bucket_id, embedding, model, dimension FROM embeddings").fetchall()
        finally:
            conn.close()
        output = []
        for bucket_id, emb_json, model, dimension in rows:
            try:
                embedding = json.loads(emb_json)
                if not isinstance(embedding, list) or not embedding:
                    continue
                if model != self.model or int(dimension) != len(embedding):
                    continue
                output.append((str(bucket_id), [float(value) for value in embedding]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return output

    def _read_labels(self) -> dict:
        with open(self.labels_path, "r", encoding="utf-8") as fh:
            body = json.load(fh)
        if not isinstance(body, dict):
            raise ValueError("labels file must contain a JSON object")
        return body

    def _write_labels(self, body: dict) -> None:
        with open(self.labels_path, "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=False, indent=2, sort_keys=True)

    def _distance_to_score(self, distance: float) -> float:
        if self.space in {"cosine", "ip"}:
            return max(-1.0, min(1.0, 1.0 - distance))
        return -distance


class EmbeddingEngine:
    """
    Embedding generation + SQLite vector storage + cosine search.
    向量生成 + SQLite 向量存储 + 余弦搜索。
    """

    def __init__(self, config: dict):
        dehy_cfg = config.get("dehydration", {})
        embed_cfg = config.get("embedding", {})

        self.api_key = embed_cfg.get("api_key") or dehy_cfg.get("api_key", "")
        self.base_url = (
            embed_cfg.get("base_url")
            or dehy_cfg.get("base_url")
            or "https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        self.model = embed_cfg.get("model", "gemini-embedding-001")
        self.enabled = bool(self.api_key) and embed_cfg.get("enabled", True)
        self.max_chars = self._int_between(embed_cfg.get("max_chars", 6000), 6000, 500, 32000)
        self.query_instruction = str(
            embed_cfg.get("query_instruction")
            or "Given a memory search query, retrieve relevant long-term memory passages."
        ).strip()
        self.document_instruction = str(embed_cfg.get("document_instruction") or "").strip()

        # --- SQLite path: buckets_dir/embeddings.db ---
        db_path = os.path.join(config["buckets_dir"], "embeddings.db")
        self.db_path = db_path

        # --- Initialize client ---
        if self.enabled:
            from openai import AsyncOpenAI

            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=30.0,
            )
        else:
            self.client = None

        # --- Initialize SQLite ---
        self._init_db()
        self.vector_index = VectorIndexHNSW(config, self.db_path, self.model)

    def _init_db(self):
        """Create embeddings table if not exists."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                model TEXT,
                dimension INTEGER,
                updated_at TEXT NOT NULL
            )
        """)
        self._ensure_column(conn, "embeddings", "model", "TEXT")
        self._ensure_column(conn, "embeddings", "dimension", "INTEGER")
        conn.commit()
        conn.close()

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        """
        Generate embedding for content and store in SQLite.
        为内容生成 embedding 并存入 SQLite。
        Returns True on success, False on failure.
        """
        if not self.enabled or not content or not content.strip():
            return False

        try:
            embedding = await self._generate_embedding(content, kind="document")
            if not embedding:
                return False
            self._store_embedding(bucket_id, embedding)
            return True
        except Exception as e:
            logger.warning(f"Embedding generation failed for {bucket_id}: {e}")
            return False

    async def _generate_embedding(self, text: str, *, kind: str = "document") -> list[float]:
        """Call API to generate embedding vector."""
        # Truncate to avoid token limits
        prepared = self._prepare_embedding_input(text, kind=kind)
        truncated = prepared[: self.max_chars]
        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=truncated,
            )
            if response.data and len(response.data) > 0:
                return response.data[0].embedding
            return []
        except Exception as e:
            logger.warning(f"Embedding API call failed: {e}")
            return []

    def _store_embedding(self, bucket_id: str, embedding: list[float]):
        """Store embedding in SQLite."""
        from utils import now_iso
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT OR REPLACE INTO embeddings (bucket_id, embedding, model, dimension, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (bucket_id, json.dumps(embedding), self.model, len(embedding), now_iso()),
        )
        conn.commit()
        conn.close()

    def delete_embedding(self, bucket_id: str):
        """Remove embedding when bucket is deleted."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM embeddings WHERE bucket_id = ?", (bucket_id,))
        conn.commit()
        conn.close()

    async def get_embedding(self, bucket_id: str) -> list[float] | None:
        """Retrieve stored embedding for a bucket. Returns None if not found."""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT embedding, model, dimension FROM embeddings WHERE bucket_id = ?", (bucket_id,)
        ).fetchone()
        conn.close()
        if row:
            try:
                embedding = json.loads(row[0])
                if not self._row_matches_current_model(row[1], row[2], embedding):
                    return None
                return embedding
            except json.JSONDecodeError:
                return None
        return None

    async def search_similar(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """
        Search for buckets similar to query text.
        Returns list of (bucket_id, similarity_score) sorted by score desc.
        搜索与查询文本相似的桶。返回 (bucket_id, 相似度分数) 列表。
        """
        query_embedding_ms = 0
        sqlite_fetch_ms = 0
        json_cosine_ms = 0
        rows_count = 0
        returned_count = 0
        vector_index_used = False
        results: list[tuple[str, float]] = []
        try:
            if not self.enabled:
                return []

            started = time.perf_counter()
            try:
                query_embedding = await self._generate_embedding(query, kind="query")
                query_embedding_ms = int((time.perf_counter() - started) * 1000)
                if not query_embedding:
                    return []
            except Exception as e:
                query_embedding_ms = int((time.perf_counter() - started) * 1000)
                logger.warning("Query embedding failed: %s", e)
                return []

            hnsw_results = None
            vector_index = getattr(self, "vector_index", None)
            if vector_index is not None and getattr(vector_index, "enabled", False):
                hnsw_results = vector_index.search(query_embedding, top_k)
            if hnsw_results is not None:
                sqlite_started = time.perf_counter()
                row_meta = self._fetch_embedding_metadata([bucket_id for bucket_id, _ in hnsw_results])
                sqlite_fetch_ms = int((time.perf_counter() - sqlite_started) * 1000)
                rows_count = len(row_meta)
                for bucket_id, score in hnsw_results:
                    model, dimension = row_meta.get(bucket_id, (None, None))
                    try:
                        if model == self.model and int(dimension) == len(query_embedding):
                            results.append((bucket_id, score))
                    except (TypeError, ValueError):
                        continue
                if results:
                    vector_index_used = True
                    returned_count = len(results[:top_k])
                    return results[:top_k]

            sqlite_started = time.perf_counter()
            rows = self._fetch_all_embedding_rows()
            sqlite_fetch_ms = int((time.perf_counter() - sqlite_started) * 1000)
            rows_count = len(rows)
            if not rows:
                return []

            cosine_started = time.perf_counter()
            for bucket_id, emb_json, model, dimension in rows:
                try:
                    stored_embedding = json.loads(emb_json)
                    if not self._row_matches_current_model(model, dimension, stored_embedding):
                        continue
                    sim = self._cosine_similarity(query_embedding, stored_embedding)
                    results.append((bucket_id, sim))
                except (json.JSONDecodeError, Exception):
                    continue

            results.sort(key=lambda x: x[1], reverse=True)
            json_cosine_ms = int((time.perf_counter() - cosine_started) * 1000)
            returned_count = len(results[:top_k])
            return results[:top_k]
        finally:
            logger.info(
                "Embedding search timing | query_embedding_ms=%s sqlite_fetch_ms=%s rows_count=%s "
                "json_cosine_ms=%s returned_count=%s vector_index_used=%s",
                query_embedding_ms,
                sqlite_fetch_ms,
                rows_count,
                json_cosine_ms,
                returned_count,
                vector_index_used,
            )

    def _fetch_all_embedding_rows(self) -> list[tuple[str, str, str | None, int | None]]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT bucket_id, embedding, model, dimension FROM embeddings").fetchall()
        finally:
            conn.close()

    def _fetch_embedding_metadata(self, bucket_ids: list[str]) -> dict[str, tuple[str | None, int | None]]:
        if not bucket_ids:
            return {}
        unique_ids = list(dict.fromkeys(str(bucket_id) for bucket_id in bucket_ids if str(bucket_id)))
        if not unique_ids:
            return {}
        placeholders = ",".join("?" for _ in unique_ids)
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                f"SELECT bucket_id, model, dimension FROM embeddings WHERE bucket_id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        finally:
            conn.close()
        return {str(bucket_id): (model, dimension) for bucket_id, model, dimension in rows}

    def _prepare_embedding_input(self, text: str, *, kind: str) -> str:
        raw = str(text or "")
        if kind == "query" and self.query_instruction:
            return f"Instruct: {self.query_instruction}\nQuery: {raw}"
        if kind == "document" and self.document_instruction:
            return f"Instruct: {self.document_instruction}\nDocument: {raw}"
        return raw

    def _row_matches_current_model(self, model: str | None, dimension: int | None, embedding: list[float]) -> bool:
        if not embedding:
            return False
        if model != self.model:
            return False
        try:
            stored_dimension = int(dimension)
        except (TypeError, ValueError):
            return False
        return stored_dimension == len(embedding)

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row[1] == column for row in rows):
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    @staticmethod
    def _int_between(value, default: int, min_value: int, max_value: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(min_value, min(max_value, number))

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
