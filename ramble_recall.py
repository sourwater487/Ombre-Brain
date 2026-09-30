"""Read-only whisper retrieval, shared by manual breath and automatic context."""
from __future__ import annotations

import asyncio
import json
import logging
import re

from recall_policy import RecallPolicy
from utils import count_tokens_approx, strip_wikilinks

logger = logging.getLogger("ombre_brain.ramble")
RAMBLE_HINT = "These are impressions you once left behind."


def is_ramble(bucket: dict) -> bool:
    meta = bucket.get("metadata") or {}
    tags = {str(tag).lower() for tag in meta.get("tags", []) or []}
    return bool(
        meta.get("type") == "feel" and "whisper" in tags
        and meta.get("keepsake_type") in (None, "", "ramble")
        and not tags.intersection({"daily_impression", "weekly_impression", "relationship_weather"})
        and meta.get("active") is not False and not meta.get("deprecated")
        and str(bucket.get("content") or "").strip()
    )


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", strip_wikilinks(str(text or ""))).casefold()


def visible_ramble_ids(buckets: list[dict], messages: list[dict]) -> set[str]:
    """Use only the supplied context, never an all-time session recall ledger."""
    texts = []
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(str(block.get("text") or block.get("input_text") or "")
                         for block in content if isinstance(block, dict)
                         and block.get("type") in {"text", "input_text"})
    joined = "\n".join(texts)
    ids = set(re.findall(r"\[bucket_id:([^\]\s]+)\]", joined))
    bodies = []
    for match in re.finditer(r"<cards>([\s\S]*?)</cards>", joined):
        try:
            payload = json.loads(match.group(1))
            cards = payload.get("cards", []) if isinstance(payload, dict) else []
            bodies.extend(_compact(card.get("content", "")) for card in cards
                          if isinstance(card, dict) and card.get("type") == "ramble")
        except (ValueError, TypeError):
            continue
    compact_context = _compact(joined)
    for bucket in buckets:
        body = _compact(bucket.get("content", ""))
        if body and (body in bodies or (len(body) >= 12 and body in compact_context)):
            ids.add(str(bucket.get("id") or ""))
    return ids


def _term_matches(term: str, text: str) -> bool:
    if re.fullmatch(r"[a-z0-9_.:-]+", term):
        return bool(re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", text))
    return term in text


async def select_rambles(
    buckets: list[dict], query: str, *, embedding_engine=None,
    policy: RecallPolicy | None = None, automatic: bool = False,
    messages: list[dict] | None = None, limit: int = 20,
    semantic_threshold: float = 0.78,
) -> list[dict]:
    candidates = [bucket for bucket in buckets if is_ramble(bucket)]
    if automatic:
        seen = visible_ramble_ids(candidates, messages or [])
        candidates = [b for b in candidates if str(b.get("id") or "") not in seen]
    if not candidates or limit <= 0:
        return []
    query = str(query or "").strip()
    if not query:
        return [] if automatic else sorted(
            candidates, key=lambda b: str(b["metadata"].get("created") or ""), reverse=True,
        )[:limit]

    policy = policy or RecallPolicy()
    terms = [term.casefold() for term in policy.specific_query_terms(query) if len(term) >= 2]
    if not terms:
        return []
    # Search within this channel before top-k, so ordinary memories cannot crowd it out.
    semantic = {}
    if embedding_engine is not None and embedding_engine.enabled:
        try:
            results = await asyncio.wait_for(embedding_engine.search_similar(
                query, top_k=min(50, len(candidates)),
                bucket_ids={str(b.get("id") or "") for b in candidates},
            ), timeout=4.0)
            semantic = dict(results)
        except Exception as exc:
            logger.warning("Ramble semantic search unavailable: %s", type(exc).__name__)

    ranked = []
    for bucket in candidates:
        body = strip_wikilinks(str(bucket.get("content") or "")).casefold()
        matches = [term for term in terms if _term_matches(term, body)]
        coverage = len(matches) / len(terms)
        score = semantic.get(str(bucket.get("id") or ""), 0.0)
        if automatic:
            # One incidental word is insufficient; semantic-only paraphrases remain possible.
            lexical_match = len(matches) >= 2 and coverage >= 0.6
            if not lexical_match and score < semantic_threshold:
                continue
        elif not matches and score < 0.55:
            continue
        ranked.append((max(score, coverage if matches else 0.0), len(matches),
                       str(bucket["metadata"].get("created") or ""), bucket))
    ranked.sort(key=lambda row: row[:3], reverse=True)
    return [row[3] for row in ranked[:limit]]


def render_rambles(buckets: list[dict], budget: int) -> tuple[str, list[str]]:
    """Keep source text verbatim (or its prefix), and include framing in the budget."""
    if budget <= 0:
        return "", []
    text, ids = RAMBLE_HINT, []
    for bucket in buckets:
        meta = bucket.get("metadata") or {}
        bucket_id = str(bucket.get("id") or "")
        created = str(meta.get("created") or meta.get("date") or "unknown")
        header = f"[bucket_id:{bucket_id}] [created:{created}] [source:ramble]"
        body = strip_wikilinks(str(bucket.get("content") or "")).strip()
        prefix = f"{text}\n\n{header}\n"
        if count_tokens_approx(prefix + body) > budget:
            low, high = 0, len(body)
            while low < high:
                mid = (low + high + 1) // 2
                if count_tokens_approx(prefix + body[:mid] + "…") <= budget:
                    low = mid
                else:
                    high = mid - 1
            if low < min(24, len(body)):
                break
            body = body[:low] + "…"
        text = prefix + body
        ids.append(bucket_id)
    return (text, ids) if ids else ("", [])
