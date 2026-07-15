from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger("ombre_brain.raw_events")

ALLOWED_RAW_ROLES = {"user", "assistant"}
RAW_EVENT_DEFAULT_SOURCE = "raw"

INJECTION_SECTION_RE = re.compile(
    r"(?im)^\s*(?:"
    r"Core Memory|Recalled Memory|Recent Context|Just Now Chat Context|"
    r"Related Memory|Dream Context|Additional private memory detail|"
    r"Long-term State Summary"
    r")\s*:?\s*$"
)
CLIENT_ATTACHMENT_RE = re.compile(r"<attachment\b[^>]*>[\s\S]*?</attachment>", re.IGNORECASE)
SELF_CLOSING_ATTACHMENT_RE = re.compile(r"<attachment\b[^>]*/>", re.IGNORECASE)
WORKSPACE_ATTACHMENT_RE = re.compile(
    r"<workspace_attachment>[\s\S]*?</workspace_attachment>",
    re.IGNORECASE,
)
LIN_MESSAGE_RE = re.compile(
    r"<lin_message\b[^>]*>\s*(?P<body>[\s\S]*?)\s*</lin_message\s*>",
    re.IGNORECASE,
)
CLIENT_CONTEXT_CONTAINER_RE = re.compile(
    r"<(?P<tag>dynamic|dynamic_context|lin_dynamic_context|ombre_live_context|memo_context)\b[^>]*>"
    r"[\s\S]*?</(?P=tag)\s*>",
    re.IGNORECASE,
)
CLIENT_CONTEXT_CONTAINER_SELF_CLOSING_RE = re.compile(
    r"<(?:dynamic|dynamic_context|lin_dynamic_context|ombre_live_context|memo_context)\b[^>]*/\s*>",
    re.IGNORECASE,
)
CLIENT_METADATA_XML_RE = re.compile(
    r"<(?P<tag>message_time|current_time|timestamp|current_weather|weather|current_location|location|geolocation)"
    r"\b[^>]*>[\s\S]*?</(?P=tag)\s*>",
    re.IGNORECASE,
)
CLIENT_METADATA_XML_SELF_CLOSING_RE = re.compile(
    r"<(?:message_time|current_time|timestamp|current_weather|weather|current_location|location|geolocation)"
    r"\b[^>]*/\s*>",
    re.IGNORECASE,
)
CLIENT_STATUS_PREFIX_RE = re.compile(
    r"^\s*\[\s*20\d{2}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?"
    r"(?:\s*\|\s*[^\]\r\n]*)*\s*\]\s*(?:\r?\n)?",
    re.IGNORECASE,
)
CLIENT_STATUS_CONTEXT_RE = re.compile(
    r"\[\s*(?P<observed_at>20\d{2}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)"
    r"(?P<details>(?:\s*\|\s*[^\]\r\n]*)+)\s*\]",
    re.IGNORECASE,
)
CLIENT_WEATHER_VALUE_RE = re.compile(
    r"<(?:current_weather|weather)\b[^>]*>([\s\S]*?)</(?:current_weather|weather)\s*>",
    re.IGNORECASE,
)
CLIENT_LOCATION_VALUE_RE = re.compile(
    r"<(?:current_location|location|geolocation)\b[^>]*>([\s\S]*?)</(?:current_location|location|geolocation)\s*>",
    re.IGNORECASE,
)
CLIENT_TIME_VALUE_RE = re.compile(
    r"<(?:message_time|current_time|timestamp)\b[^>]*>([\s\S]*?)</(?:message_time|current_time|timestamp)\s*>",
    re.IGNORECASE,
)
CLIENT_WEATHER_HINT_RE = re.compile(
    r"(?:天气|气温|温度|湿度|体感|晴|阴|多云|雨|雪|雷|雾|霾|风|台风|℃|°C|℉|°F)",
    re.IGNORECASE,
)
CLIENT_CONTEXT_BLOCK_TITLES = {
    "当前时间",
    "当前电量",
    "当前天气",
    "当前位置",
    "当前屏幕应用",
    "应用使用时长",
    "最近通知",
    "相关记忆",
    "屏幕文本",
}
STICKER_PAYLOAD_KEYS = {"type", "id"}
STICKER_PAYLOAD_MAX_CHARS = 4096


def is_sticker_payload(text: Any) -> bool:
    """Return true only for the frontend's complete, standalone sticker JSON."""
    raw = str(text or "").strip()
    if (
        not raw
        or len(raw) > STICKER_PAYLOAD_MAX_CHARS
        or not raw.startswith("{")
        or not raw.endswith("}")
    ):
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or set(payload) != STICKER_PAYLOAD_KEYS:
        return False
    sticker_id = payload.get("id")
    return payload.get("type") == "sticker" and isinstance(sticker_id, str) and bool(sticker_id.strip())


def strip_raw_client_context(text: str, *, strip_injected_xml: bool = True) -> str:
    cleaned = str(text or "")
    if strip_injected_xml:
        lin_message = _extract_provider_lin_message(cleaned)
        if lin_message is not None:
            # The provider envelope is the strongest boundary: everything
            # outside lin_message is injected context, not the user's utterance.
            cleaned = lin_message
        else:
            # Other clients may send injected XML next to plain user text
            # without a lin_message envelope.
            cleaned = _strip_client_context_containers(cleaned)
        cleaned = CLIENT_METADATA_XML_RE.sub("", cleaned)
        cleaned = CLIENT_METADATA_XML_SELF_CLOSING_RE.sub("", cleaned)
        while CLIENT_STATUS_PREFIX_RE.match(cleaned):
            cleaned = CLIENT_STATUS_PREFIX_RE.sub("", cleaned, count=1)
    cleaned = WORKSPACE_ATTACHMENT_RE.sub("", cleaned)
    cleaned = CLIENT_ATTACHMENT_RE.sub("", cleaned)
    cleaned = SELF_CLOSING_ATTACHMENT_RE.sub("", cleaned)
    cleaned = _strip_client_context_blocks(cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    return "" if is_sticker_payload(cleaned) else cleaned


def extract_raw_client_context(text: str, *, max_value_chars: int = 240) -> dict[str, str]:
    """Extract bounded app-provided weather/location without treating it as speech."""
    raw = str(text or "")
    if not raw:
        return {}
    limit = max(32, min(2000, int(max_value_chars or 240)))

    def clean_value(value: Any) -> str:
        cleaned = re.sub(r"<[^>]+>", " ", str(value or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned[:limit].rstrip()

    weather_values = [clean_value(value) for value in CLIENT_WEATHER_VALUE_RE.findall(raw)]
    location_values = [clean_value(value) for value in CLIENT_LOCATION_VALUE_RE.findall(raw)]
    time_values = [clean_value(value) for value in CLIENT_TIME_VALUE_RE.findall(raw)]
    weather = next((value for value in reversed(weather_values) if value), "")
    location = next((value for value in reversed(location_values) if value), "")
    observed_at = next((value for value in reversed(time_values) if value), "")

    status_matches = list(CLIENT_STATUS_CONTEXT_RE.finditer(raw))
    if status_matches:
        match = status_matches[-1]
        observed_at = observed_at or clean_value(match.group("observed_at"))
        details = [clean_value(value) for value in match.group("details").split("|")]
        details = [value for value in details if value]
        if details:
            status_weather = [value for value in details if CLIENT_WEATHER_HINT_RE.search(value)]
            status_location = [value for value in details if value not in status_weather]
            if status_weather:
                weather = weather or " | ".join(status_weather)
            if status_location:
                location = location or " | ".join(status_location)

    result = {}
    if weather:
        result["weather"] = weather
    if location:
        result["location"] = location
    if result and observed_at:
        result["observed_at"] = observed_at[:80]
    return result


def _extract_provider_lin_message(text: str) -> str | None:
    matches = list(LIN_MESSAGE_RE.finditer(str(text or "")))
    if not matches:
        return None
    match = matches[-1]
    surrounding = f"{text[:match.start()]}\n{text[match.end():]}"
    surrounding = _strip_client_context_containers(surrounding)
    surrounding = CLIENT_METADATA_XML_RE.sub("", surrounding)
    surrounding = CLIENT_METADATA_XML_SELF_CLOSING_RE.sub("", surrounding)
    surrounding = WORKSPACE_ATTACHMENT_RE.sub("", surrounding)
    surrounding = CLIENT_ATTACHMENT_RE.sub("", surrounding)
    surrounding = SELF_CLOSING_ATTACHMENT_RE.sub("", surrounding)
    if surrounding.strip():
        # A literal lin_message mentioned inside ordinary prose is user text,
        # not a provider envelope.
        return None
    return match.group("body")


def _strip_client_context_containers(text: str) -> str:
    cleaned = str(text or "")
    # A small bounded loop handles wrappers nested inside another reserved
    # wrapper without turning this into a general-purpose XML parser.
    for _ in range(6):
        previous = cleaned
        cleaned = CLIENT_CONTEXT_CONTAINER_RE.sub("", cleaned)
        cleaned = CLIENT_CONTEXT_CONTAINER_SELF_CLOSING_RE.sub("", cleaned)
        if cleaned == previous:
            break
    return cleaned


def _strip_client_context_blocks(text: str) -> str:
    kept: list[str] = []
    skipping = False
    for line in str(text or "").splitlines():
        stripped = line.strip()
        title = ""
        if stripped.startswith("【") and "】" in stripped:
            title = stripped[1 : stripped.index("】")].strip()
        if title:
            skipping = title in CLIENT_CONTEXT_BLOCK_TITLES
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def raw_event_text_looks_injected(text: str, raw: dict[str, Any] | None = None) -> bool:
    raw = raw or {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    flags = {
        str(raw.get("kind") or "").lower(),
        str(raw.get("source_type") or "").lower(),
        str(metadata.get("kind") or "").lower(),
        str(metadata.get("source_type") or "").lower(),
    }
    if flags & {"injection", "memory_injection", "tool", "tool_result", "system", "developer"}:
        return True
    stripped = str(text or "").strip()
    if stripped.startswith("Live private context for the current turn"):
        return True
    if INJECTION_SECTION_RE.search(stripped):
        return True
    return "[bucket_id:" in stripped and any(
        marker in stripped
        for marker in (
            "Recalled Memory",
            "Related Memory",
            "Recent Context",
            "Core Memory",
        )
    )


class RawEventStore:
    """Append-only-ish raw dialogue archive with optional FTS search."""

    def __init__(self, config: dict):
        config = config or {}
        raw_cfg = config.get("raw_events", {}) if isinstance(config.get("raw_events", {}), dict) else {}
        state_dir = config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(config.get("buckets_dir", "buckets"))),
            "state",
        )
        self.db_path = str(raw_cfg.get("db_path") or os.path.join(state_dir, "raw_events.sqlite"))
        self.max_ingest_batch = max(1, min(5000, int(raw_cfg.get("max_ingest_batch", 1000))))
        self.client_context_enabled = self._bool_config_value(
            raw_cfg.get("client_context_enabled"),
            True,
        )
        self.client_context_max_records = max(
            0,
            min(100000, int(raw_cfg.get("client_context_max_records", 5000))),
        )
        self.client_context_max_per_day = max(
            1,
            min(500, int(raw_cfg.get("client_context_max_per_day", 24))),
        )
        self.client_context_value_max_chars = max(
            32,
            min(2000, int(raw_cfg.get("client_context_value_max_chars", 240))),
        )
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.fts_enabled = False
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_event_id TEXT NOT NULL DEFAULT '',
                event_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                conversation_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                client TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(source, event_hash)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_client_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_event_id INTEGER,
                context_hash TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                date_key TEXT NOT NULL,
                weather TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_client_context_date "
            "ON raw_client_context(date_key, id DESC)"
        )
        self._prune_client_context_rows(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_events_created ON raw_events(created_at DESC, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_events_source ON raw_events(source, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_events_role ON raw_events(role, created_at DESC)")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_events_source_event_id
            ON raw_events(source, source_event_id)
            WHERE source_event_id != ''
            """
        )
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS raw_events_fts
                USING fts5(text, source, conversation_id, session_id, content='raw_events', content_rowid='id')
                """
            )
            self.fts_enabled = True
        except sqlite3.OperationalError as exc:
            self.fts_enabled = False
            logger.warning("raw_events FTS5 disabled: %s", exc)
        conn.commit()
        conn.close()

    def _prune_client_context_rows(self, conn: sqlite3.Connection) -> None:
        if self.client_context_max_records <= 0:
            return
        stale_global = conn.execute(
            "SELECT id FROM raw_client_context ORDER BY id DESC LIMIT -1 OFFSET ?",
            (self.client_context_max_records,),
        ).fetchall()
        if stale_global:
            conn.executemany(
                "DELETE FROM raw_client_context WHERE id = ?",
                [(int(row["id"]),) for row in stale_global],
            )
        date_keys = conn.execute(
            "SELECT date_key FROM raw_client_context GROUP BY date_key "
            "HAVING COUNT(*) > ?",
            (self.client_context_max_per_day,),
        ).fetchall()
        for row in date_keys:
            stale_daily = conn.execute(
                "SELECT id FROM raw_client_context WHERE date_key = ? "
                "ORDER BY id DESC LIMIT -1 OFFSET ?",
                (str(row["date_key"]), self.client_context_max_per_day),
            ).fetchall()
            if stale_daily:
                conn.executemany(
                    "DELETE FROM raw_client_context WHERE id = ?",
                    [(int(item["id"]),) for item in stale_daily],
                )

    def ingest(self, events: list[dict[str, Any]], *, source: str = "") -> dict[str, Any]:
        safe_source = self._clean_source(source)
        now = self._now_iso()
        items = []
        inserted = 0
        duplicate = 0
        rejected = 0
        client_context_inserted = 0
        for raw in list(events or [])[: self.max_ingest_batch]:
            raw_text = self._coerce_text((raw or {}).get("text", (raw or {}).get("content", "")))
            normalized, reason = self._normalize_event(raw, default_source=safe_source, ingested_at=now)
            if reason:
                rejected += 1
                items.append(
                    {
                        "status": "rejected",
                        "reason": reason,
                        "source_event_id": str((raw or {}).get("source_event_id") or (raw or {}).get("id") or ""),
                    }
                )
                continue
            status, row_id = self._insert_event(normalized)
            if status == "inserted":
                inserted += 1
            else:
                duplicate += 1
            if normalized["role"] == "user":
                context_result = self.record_client_context(
                    raw_text,
                    source=normalized["source"],
                    session_id=normalized["session_id"],
                    created_at=normalized["created_at"],
                    raw_event_id=row_id,
                )
                if context_result.get("status") == "inserted":
                    client_context_inserted += 1
            items.append(
                {
                    "status": status,
                    "id": row_id,
                    "source": normalized["source"],
                    "source_event_id": normalized["source_event_id"],
                    "role": normalized["role"],
                }
            )
        return {
            "ok": True,
            "inserted": inserted,
            "duplicate": duplicate,
            "rejected": rejected,
            "client_context_inserted": client_context_inserted,
            "items": items,
        }

    def record_client_context(
        self,
        text: str,
        *,
        source: str = "",
        session_id: str = "",
        created_at: str = "",
        date_key: str = "",
        raw_event_id: int | None = None,
    ) -> dict[str, Any]:
        if not self.client_context_enabled or self.client_context_max_records <= 0:
            return {"status": "disabled"}
        context = extract_raw_client_context(
            text,
            max_value_chars=self.client_context_value_max_chars,
        )
        if not context:
            return {"status": "empty"}
        safe_created_at = self._clean_time(created_at or context.get("observed_at") or self._now_iso())
        safe_date_key = self._clean_date_key(
            date_key or context.get("observed_at") or safe_created_at
        )
        fingerprint = {
            "date_key": safe_date_key,
            "weather": context.get("weather", ""),
            "location": context.get("location", ""),
        }
        context_hash = hashlib.sha256(
            json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return self._insert_client_context(
            raw_event_id=raw_event_id,
            context_hash=context_hash,
            source=self._clean_source(source),
            session_id=str(session_id or "")[:160],
            created_at=safe_created_at,
            date_key=safe_date_key,
            weather=str(context.get("weather") or ""),
            location=str(context.get("location") or ""),
        )

    def list_client_context_for_date(self, date_key: str, *, limit: int = 24) -> list[dict[str, Any]]:
        safe_date_key = self._clean_date_key(date_key, fallback_today=False)
        try:
            safe_limit = max(0, min(500, int(limit)))
        except (TypeError, ValueError):
            safe_limit = 24
        if not safe_date_key or safe_limit <= 0:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, raw_event_id, source, session_id, created_at, date_key, weather, location
                FROM raw_client_context
                WHERE date_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_date_key, safe_limit),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def _insert_client_context(
        self,
        *,
        raw_event_id: int | None,
        context_hash: str,
        source: str,
        session_id: str,
        created_at: str,
        date_key: str,
        weather: str,
        location: str,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT id FROM raw_client_context WHERE context_hash = ? LIMIT 1",
                (context_hash,),
            ).fetchone()
            if existing:
                return {"status": "duplicate", "id": int(existing["id"])}

            same_day_rows = conn.execute(
                "SELECT id FROM raw_client_context WHERE date_key = ? ORDER BY id DESC",
                (date_key,),
            ).fetchall()
            stale_same_day = same_day_rows[max(0, self.client_context_max_per_day - 1) :]
            if stale_same_day:
                conn.executemany(
                    "DELETE FROM raw_client_context WHERE id = ?",
                    [(int(row["id"]),) for row in stale_same_day],
                )

            cursor = conn.execute(
                """
                INSERT INTO raw_client_context
                (raw_event_id, context_hash, source, session_id, created_at, date_key, weather, location)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(raw_event_id) if raw_event_id else None,
                    context_hash,
                    source,
                    session_id,
                    created_at,
                    date_key,
                    weather,
                    location,
                ),
            )
            row_id = int(cursor.lastrowid or 0)
            conn.execute(
                """
                DELETE FROM raw_client_context
                WHERE id NOT IN (
                    SELECT id FROM raw_client_context ORDER BY id DESC LIMIT ?
                )
                """,
                (self.client_context_max_records,),
            )
            conn.commit()
            return {"status": "inserted", "id": row_id, "date_key": date_key}
        finally:
            conn.close()

    def search(
        self,
        query: str = "",
        *,
        limit: int = 10,
        source: str = "",
        role: str = "",
        conversation_id: str = "",
        session_id: str = "",
        since: str = "",
        until: str = "",
    ) -> dict[str, Any]:
        safe_limit = max(1, min(100, int(limit or 10)))
        cleaned_query = strip_raw_client_context(str(query or ""))
        filters, params = self._search_filters(
            source=source,
            role=role,
            conversation_id=conversation_id,
            session_id=session_id,
            since=since,
            until=until,
        )
        rows = self._search_fts(cleaned_query, filters, params, safe_limit) if cleaned_query else []
        if len(rows) < safe_limit:
            rows = self._merge_rows(
                rows,
                self._search_like(cleaned_query, filters, params, safe_limit) if cleaned_query else self._search_recent(filters, params, safe_limit),
                safe_limit,
            )
        return {
            "ok": True,
            "query": cleaned_query,
            "count": len(rows),
            "items": [self._row_to_event(row) for row in rows],
        }

    def list_events_between(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        limit: int = 40,
        source: str = "",
        conversation_id: str = "",
        session_id: str = "",
    ) -> list[dict[str, Any]]:
        try:
            raw_limit = int(limit)
        except (TypeError, ValueError):
            raw_limit = 40
        safe_limit = max(0, min(10000, raw_limit))
        filters, params = self._search_filters(
            source=source,
            conversation_id=conversation_id,
            session_id=session_id,
        )
        conn = self._connect()
        if safe_limit > 0:
            rows = conn.execute(
                f"""
                SELECT e.*
                FROM raw_events e
                WHERE 1 = 1 {filters}
                ORDER BY e.id DESC
                LIMIT ?
                """,
                [*params, max(safe_limit, 500)],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT e.*
                FROM raw_events e
                WHERE 1 = 1 {filters}
                ORDER BY e.id DESC
                """,
                params,
            ).fetchall()
        conn.close()

        compare_tz = start_at.tzinfo or end_at.tzinfo

        def parse_local(value: Any) -> datetime | None:
            try:
                parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            except ValueError:
                return None
            if compare_tz is None:
                return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=compare_tz)
            return parsed.astimezone(compare_tz)

        start = start_at
        end = end_at
        if compare_tz is not None:
            if start.tzinfo is None:
                start = start.replace(tzinfo=compare_tz)
            else:
                start = start.astimezone(compare_tz)
            if end.tzinfo is None:
                end = end.replace(tzinfo=compare_tz)
            else:
                end = end.astimezone(compare_tz)
        elif start.tzinfo is not None:
            start = start.replace(tzinfo=None)
        elif end.tzinfo is not None:
            end = end.replace(tzinfo=None)

        selected: list[dict[str, Any]] = []
        for row in rows:
            created = parse_local(row["created_at"])
            if created is None or not (start <= created < end):
                continue
            selected.append(self._row_to_event(row))
            if safe_limit > 0 and len(selected) >= safe_limit:
                break
        return selected

    def _insert_event(self, event: dict[str, Any]) -> tuple[str, int | None]:
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO raw_events
                (source, source_event_id, event_hash, role, text, created_at, ingested_at,
                 conversation_id, session_id, client, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["source"],
                    event["source_event_id"],
                    event["event_hash"],
                    event["role"],
                    event["text"],
                    event["created_at"],
                    event["ingested_at"],
                    event["conversation_id"],
                    event["session_id"],
                    event["client"],
                    event["metadata_json"],
                ),
            )
            if cursor.rowcount:
                row_id = int(cursor.lastrowid or 0)
                if self.fts_enabled:
                    try:
                        conn.execute(
                            """
                            INSERT INTO raw_events_fts(rowid, text, source, conversation_id, session_id)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                row_id,
                                event["text"],
                                event["source"],
                                event["conversation_id"],
                                event["session_id"],
                            ),
                        )
                    except sqlite3.OperationalError as exc:
                        logger.warning("raw_events FTS insert failed: %s", exc)
                conn.commit()
                return "inserted", row_id
            conn.commit()
            row_id = self._find_existing_id(conn, event)
            return "duplicate", row_id
        finally:
            conn.close()

    def _find_existing_id(self, conn: sqlite3.Connection, event: dict[str, Any]) -> int | None:
        if event.get("source_event_id"):
            row = conn.execute(
                "SELECT id FROM raw_events WHERE source = ? AND source_event_id = ? LIMIT 1",
                (event["source"], event["source_event_id"]),
            ).fetchone()
            if row:
                return int(row["id"])
        row = conn.execute(
            "SELECT id FROM raw_events WHERE source = ? AND event_hash = ? LIMIT 1",
            (event["source"], event["event_hash"]),
        ).fetchone()
        return int(row["id"]) if row else None

    def _normalize_event(
        self,
        raw: dict[str, Any] | None,
        *,
        default_source: str,
        ingested_at: str,
    ) -> tuple[dict[str, Any] | None, str]:
        if not isinstance(raw, dict):
            return None, "invalid_event"
        role = str(raw.get("role") or "").strip().lower()
        if role not in ALLOWED_RAW_ROLES:
            return None, "invalid_role"
        text = strip_raw_client_context(
            self._coerce_text(raw.get("text", raw.get("content", ""))),
            strip_injected_xml=role == "user",
        )
        if not text:
            return None, "empty_text"
        if self._looks_injected(text, raw):
            return None, "injected_context"

        source = self._clean_source(raw.get("source") or default_source)
        source_event_id = str(raw.get("source_event_id") or raw.get("event_id") or raw.get("id") or "").strip()
        conversation_id = str(raw.get("conversation_id") or raw.get("thread_id") or "").strip()
        session_id = str(raw.get("session_id") or "").strip()
        client = str(raw.get("client") or "").strip()
        created_at = self._clean_time(raw.get("created_at") or raw.get("timestamp") or raw.get("time") or ingested_at)
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        event_hash = self._event_hash(
            source=source,
            source_event_id=source_event_id,
            role=role,
            text=text,
            created_at=created_at,
            conversation_id=conversation_id,
            session_id=session_id,
        )
        return {
            "source": source,
            "source_event_id": source_event_id,
            "event_hash": event_hash,
            "role": role,
            "text": text,
            "created_at": created_at,
            "ingested_at": ingested_at,
            "conversation_id": conversation_id,
            "session_id": session_id,
            "client": client,
            "metadata_json": metadata_json,
        }, ""

    @staticmethod
    def _clean_source(value: Any) -> str:
        text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or RAW_EVENT_DEFAULT_SOURCE).strip())
        return text[:80] or RAW_EVENT_DEFAULT_SOURCE

    @staticmethod
    def _clean_time(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return RawEventStore._now_iso()
        return text[:80]

    @staticmethod
    def _clean_date_key(value: Any, *, fallback_today: bool = True) -> str:
        text = str(value or "").strip()
        match = re.search(r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", text)
        if match:
            year, month, day = (int(part) for part in match.groups())
            try:
                return datetime(year, month, day).date().isoformat()
            except ValueError:
                pass
        return datetime.now(timezone.utc).date().isoformat() if fallback_today else ""

    @staticmethod
    def _bool_config_value(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _coerce_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    item_type = str(item.get("type") or "").lower()
                    if item_type in {"tool_result", "tool_use", "function_call", "function_result"}:
                        continue
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(part for part in parts if part)
        return str(value or "")

    @staticmethod
    def _looks_injected(text: str, raw: dict[str, Any]) -> bool:
        return raw_event_text_looks_injected(text, raw)

    @staticmethod
    def _event_hash(**parts: str) -> str:
        payload = json.dumps(parts, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        metadata = {}
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        return {
            "id": row["id"],
            "source": row["source"],
            "source_event_id": row["source_event_id"],
            "role": row["role"],
            "text": row["text"],
            "created_at": row["created_at"],
            "ingested_at": row["ingested_at"],
            "conversation_id": row["conversation_id"],
            "session_id": row["session_id"],
            "client": row["client"],
            "metadata": metadata,
        }

    def _search_filters(
        self,
        *,
        source: str = "",
        role: str = "",
        conversation_id: str = "",
        session_id: str = "",
        since: str = "",
        until: str = "",
    ) -> tuple[str, list[Any]]:
        clauses = []
        params: list[Any] = []
        if source:
            clauses.append("e.source = ?")
            params.append(self._clean_source(source))
        role = str(role or "").strip().lower()
        if role in ALLOWED_RAW_ROLES:
            clauses.append("e.role = ?")
            params.append(role)
        if conversation_id:
            clauses.append("e.conversation_id = ?")
            params.append(str(conversation_id))
        if session_id:
            clauses.append("e.session_id = ?")
            params.append(str(session_id))
        if since:
            clauses.append("e.created_at >= ?")
            params.append(str(since))
        if until:
            clauses.append("e.created_at <= ?")
            params.append(str(until))
        return (" AND " + " AND ".join(clauses)) if clauses else "", params

    def _search_fts(self, query: str, filters: str, params: list[Any], limit: int) -> list[sqlite3.Row]:
        if not self.fts_enabled or not query:
            return []
        match = '"' + query.replace('"', '""') + '"'
        conn = self._connect()
        try:
            return conn.execute(
                f"""
                SELECT e.*
                FROM raw_events_fts f
                JOIN raw_events e ON e.id = f.rowid
                WHERE raw_events_fts MATCH ? {filters}
                ORDER BY bm25(raw_events_fts), e.created_at DESC, e.id DESC
                LIMIT ?
                """,
                [match, *params, limit],
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def _search_like(self, query: str, filters: str, params: list[Any], limit: int) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(
                f"""
                SELECT e.*
                FROM raw_events e
                WHERE e.text LIKE ? {filters}
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT ?
                """,
                [f"%{query}%", *params, limit],
            ).fetchall()
        finally:
            conn.close()

    def _search_recent(self, filters: str, params: list[Any], limit: int) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(
                f"""
                SELECT e.*
                FROM raw_events e
                WHERE 1 = 1 {filters}
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        finally:
            conn.close()

    @staticmethod
    def _merge_rows(first: list[sqlite3.Row], second: list[sqlite3.Row], limit: int) -> list[sqlite3.Row]:
        rows = []
        seen = set()
        for row in [*(first or []), *(second or [])]:
            row_id = int(row["id"])
            if row_id in seen:
                continue
            seen.add(row_id)
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows
