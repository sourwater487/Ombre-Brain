"""Read-only RiJi retrieval; automatic admission requires a real rerank >= 0.90."""
from __future__ import annotations

import asyncio
import math
import re

import httpx

from memory_relevance import EMOTIONAL_RECALL_STATE_TERMS
from utils import count_tokens_approx

DIARY_RERANK_THRESHOLD = 0.90
RIJI_BASE_URL = "https://riji.lin-che.com/diaries"
DIARY_PAGE_URL = "https://app.lin-che.com/diary"


def topic_terms(query, policy):
    excluded = {str(t).casefold() for t in EMOTIONAL_RECALL_STATE_TERMS}
    excluded.update({"日记", "记忆", "回忆", "diary", "journal", "难过", "开心", "伤心", "高兴", "生气", "焦虑", "孤独", "sad", "happy", "angry", "lonely", "anxious"})
    terms = sorted({str(t).strip().casefold() for t in policy.specific_query_terms(query)
                    if 2 <= len(str(t).strip()) <= 80}, key=lambda t: (-len(t), t))
    return [t for t in terms if t not in excluded and not any(t != other and t in other for other in terms)][:4]


def matches(term, text):
    if re.fullmatch(r"[a-z0-9_.:-]+", term):
        return bool(re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", text.casefold()))
    return term in text.casefold()


def visible_text(messages):
    parts = []
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(b.get("text") or b.get("input_text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") in {"text", "input_text"})
    return "\n".join(parts)


def fragments(rows, terms, visible):
    candidates = []
    seen = set()
    for row in rows:
        diary_id = row.get("id")
        if not isinstance(diary_id, int) or isinstance(diary_id, bool) or diary_id <= 0:
            continue
        content = str(row.get("content") or "")
        # Preserve complete original sentences; do not invent summaries or clip assertions.
        for sentence in re.split(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+|\n+", content):
            sentence = sentence.strip()
            if not 12 <= len(sentence) <= 600 or sentence in visible:
                continue
            anchors = sum(matches(term, sentence) for term in terms)
            key = (diary_id, sentence)
            if not anchors or key in seen:
                continue
            seen.add(key)
            candidates.append({"row": row, "text": sentence, "anchors": anchors})
    candidates.sort(key=lambda c: (-c["anchors"], -c["row"]["id"]))
    # Round-robin diaries so a long entry cannot consume every rerank slot.
    groups = {}
    for item in candidates:
        groups.setdefault(item["row"]["id"], []).append(item)
    result = []
    for offset in range(2):
        for group in groups.values():
            if offset < len(group):
                result.append(group[offset])
    return result[:20]


async def _retrieve(query, *, policy, reranker, messages, budget, client):
    debug = {"status": "skipped", "threshold": DIARY_RERANK_THRESHOLD}
    terms = topic_terms(query, policy)
    if not terms or budget <= 0 or not getattr(reranker, "enabled", False):
        return "", {**debug, "reason": "no_topic_budget_or_reranker"}

    async def search(term):
        response = await client.post(RIJI_BASE_URL + "/search", json={"keyword": term, "limit": 30})
        response.raise_for_status()
        body = response.json()
        return [row for row in body.get("diaries", []) if isinstance(row, dict)]

    results = await asyncio.gather(*(search(term) for term in terms))
    candidates = fragments([row for rows in results for row in rows], terms, visible_text(messages))
    if not candidates:
        return "", {**debug, "reason": "no_new_topic_fragment"}
    # Score exactly the fragment that may be injected, not an unrelated whole diary.
    scores = await reranker.rerank(query, [c["text"] for c in candidates], top_n=len(candidates))
    text, selected, ids = "", [], set()
    for result in sorted(scores, key=lambda r: r.score, reverse=True):
        if not math.isfinite(result.score) or result.score < DIARY_RERANK_THRESHOLD or result.score > 1:
            continue
        if not 0 <= result.index < len(candidates):
            continue
        item = candidates[result.index]
        diary_id = item["row"]["id"]
        if diary_id in ids:
            continue
        # Re-read the selected original: edits/deletions during retrieval must not leak stale text.
        response = await client.get(f"{RIJI_BASE_URL}/{diary_id}")
        if response.status_code == 404:
            continue
        response.raise_for_status()
        fresh = response.json()
        if fresh.get("id") != diary_id or item["text"] not in str(fresh.get("content") or ""):
            continue
        date = str(fresh.get("date") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        author = str(fresh.get("author") or "")
        if author not in {"ai", "user"}:
            continue
        link = f"{DIARY_PAGE_URL}?entry={diary_id}"
        block = f"[diary_id:{diary_id}] [date:{date}] [author:{author}]\n[RiJi · {date}]({link})\n{item['text']}"
        next_text = (text + "\n\n" + block).strip()
        if count_tokens_approx(next_text) > budget:
            continue
        text = next_text
        ids.add(diary_id)
        selected.append({"id": diary_id, "score": result.score, "source_url": link})
        if len(ids) >= 2:
            break
    return text, {**debug, "status": "injected" if text else "skipped",
                  "reason": "qualified" if text else "no_qualified_fragment",
                  "candidate_count": len(candidates), "selected": selected}


async def recall_diaries(query, *, policy, reranker, messages, budget, client=None):
    try:
        async with asyncio.timeout(10):
            if client is not None:
                return await _retrieve(query, policy=policy, reranker=reranker,
                                       messages=messages, budget=budget, client=client)
            async with httpx.AsyncClient(timeout=4) as source_client:
                return await _retrieve(query, policy=policy, reranker=reranker,
                                       messages=messages, budget=budget, client=source_client)
    except Exception as exc:
        # Do not log diary bodies or network response bodies.
        return "", {"status": "skipped", "reason": "retrieval_unavailable",
                    "error_type": type(exc).__name__, "threshold": DIARY_RERANK_THRESHOLD}
