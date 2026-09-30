import ast
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from embedding_engine import EmbeddingEngine
from ramble_recall import RAMBLE_HINT, render_rambles, select_rambles, visible_ramble_ids
from utils import count_tokens_approx


def bucket(key, body, created="2026-01-01T12:00:00+08:00", **metadata):
    return {"id": key, "content": body, "metadata": {
        "type": "feel", "tags": ["whisper"], "created": created, **metadata}}


class RambleTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_topic_beats_recency_and_is_channel_scoped(self):
        rows = [bucket("old", "海边散步时，我想起了那片灯光。"),
                bucket("new", "我想学会烘焙面包。", "2026-09-01"),
                bucket("ordinary", "海边散步", type="dynamic"),
                bucket("daily", "海边散步", tags=["whisper", "daily_impression"]),
                bucket("legacy-note", "海边散步", keepsake_type="note"),
                bucket("inactive", "海边散步", active=False)]
        self.assertEqual([b["id"] for b in await select_rambles(rows, "海边散步")], ["old"])
        self.assertEqual([b["id"] for b in await select_rambles(rows, "", limit=1)], ["new"])
        self.assertEqual(await select_rambles(rows, "量子计算机"), [])
        self.assertEqual(await select_rambles(rows, "", automatic=True), [])

    async def test_automatic_requires_more_than_incidental_overlap(self):
        policy = SimpleNamespace(specific_query_terms=lambda _: ["ocean", "walking", "lights"])
        rows = [bucket("incidental", "The ocean reminds me of programming."),
                bucket("related", "I remember walking beside the ocean lights.")]
        selected = await select_rambles(rows, "topic", policy=policy, automatic=True, limit=1)
        self.assertEqual([b["id"] for b in selected], ["related"])
        self.assertEqual(await select_rambles(rows[:1], "topic", policy=policy, automatic=True), [])

    async def test_semantic_paraphrase_and_failure_fallback(self):
        row = bucket("old", "我想把那些没有说出口的话，留在分别的夜里。")
        engine = SimpleNamespace(enabled=True, search_similar=AsyncMock(return_value=[("old", .86)]))
        results = await select_rambles([row], "离别时的沉默", embedding_engine=engine, automatic=True)
        self.assertEqual(results, [row])
        self.assertEqual(engine.search_similar.call_args.kwargs["bucket_ids"], {"old"})
        engine.search_similar.side_effect = RuntimeError("fixture")
        self.assertEqual(await select_rambles([row], "量子计算机", embedding_engine=engine, automatic=True), [])

    async def test_context_dedupe_and_compaction_readmission(self):
        row = bucket("a", "我想记住海边散步时那片温暖的灯光。")
        context, _ = render_rambles([row], 300)
        messages = [{"role": "user", "content": [{"type": "text", "text": context}]}]
        self.assertEqual(visible_ramble_ids([row], messages), {"a"})
        self.assertEqual(await select_rambles([row], "海边散步", automatic=True, messages=messages), [])
        self.assertEqual([r["id"] for r in await select_rambles(
            [row], "海边散步", automatic=True, messages=[])], ["a"])
        # Manual lookup deliberately ignores automatic visibility suppression.
        self.assertEqual(await select_rambles([row], "海边散步", messages=messages), [row])
        cards = json.dumps({"cards": [{"type": "ramble", "content": row["content"]}]})
        self.assertEqual(visible_ramble_ids([row], [{"role": "assistant", "content": f"<cards>{cards}</cards>"}]), {"a"})

    def test_budget_includes_hint_metadata_and_separators(self):
        rows = [bucket("a", "我想把海边散步时的灯光记下来。" * 100), bucket("b", "第二条。")]
        for budget in (0, 10, 50, 100, 320):
            rendered, ids = render_rambles(rows, budget)
            self.assertLessEqual(count_tokens_approx(rendered), budget)
            if ids:
                self.assertIn(RAMBLE_HINT, rendered)
                self.assertIn("[created:2026-01-01T12:00:00+08:00] [source:ramble]", rendered)
        self.assertEqual(render_rambles(rows, 0), ("", []))

    async def test_vector_filter_precedes_top_k(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = EmbeddingEngine({"buckets_dir": directory, "embedding": {"enabled": False, "model": "fixture"}})
            engine.enabled = True
            engine._generate_embedding = AsyncMock(return_value=[1.0, 0.0])
            engine._store_embedding("ordinary", [1.0, 0.0])
            engine._store_embedding("ramble", [.8, .6])
            result = await engine.search_similar("fixture", top_k=1, bucket_ids={"ramble"})
            self.assertEqual(result[0][0], "ramble")
            self.assertEqual((await engine.search_similar("fixture", top_k=1))[0][0], "ordinary")
            engine._generate_embedding.reset_mock()
            self.assertEqual(await engine.search_similar("fixture", bucket_ids=set()), [])
            engine._generate_embedding.assert_not_awaited()


class GatewayRambleTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pipeline_visibility_sessions_continuation_and_total_budget(self):
        from gateway import GatewayService
        with tempfile.TemporaryDirectory() as directory:
            service = GatewayService({
                "buckets_dir": directory, "state_dir": directory,
                "gateway": {"upstreams": [], "recalled_memory_budget": 0,
                            "related_memory_budget": 0, "core_memory_budget": 0,
                            "recent_context_budget": 0, "ramble_memory_budget": 160,
                            "inject_total_budget": 600},
                "persona": {"enabled": False}, "embedding": {"enabled": False},
            })
            try:
                row = bucket("ramble-fixture", "海边散步的灯光让我想起了那些安静的夜晚。")
                service._list_gateway_buckets = AsyncMock(return_value=[row])
                service._auto_recall_low_signal_query = lambda _: False
                service._route_memory_sentinel = AsyncMock(return_value={"route": "skip"})
                payload = {"model": "cc-fixture", "messages": [{"role": "user", "content": "海边散步"}]}

                async def prepare(messages=None, session="a", external=True):
                    return await service.prepare_payload(
                        {**payload, "messages": messages or payload["messages"]}, session,
                        include_debug=True, external_transport=external)

                prepared, ids, debug = await prepare()
                self.assertEqual(ids, ["ramble-fixture"], debug.get("ramble_recall_debug"))
                self.assertEqual(debug["ramble_recall_debug"]["status"], "injected")
                self.assertIn("ramble-fixture", debug["injected_bucket_ids"])
                self.assertIn(RAMBLE_HINT, str(prepared["messages"]))
                self.assertLessEqual(count_tokens_approx(debug["stable_context"]) +
                                     count_tokens_approx(debug["dynamic_context"]), service.inject_total_budget)
                history = prepared["messages"] + [{"role": "assistant", "content": "好。"},
                                                    {"role": "user", "content": "再聊聊海边散步"}]
                _, ids, debug = await prepare(history)
                self.assertNotIn("ramble-fixture", ids or [])
                self.assertEqual(debug["ramble_memory"], "")
                # A separate session or compacted history may recall the item again.
                for session in ("b", "a"):
                    _, ids, _ = await prepare(session=session)
                    self.assertIn("ramble-fixture", ids)
                # Same preparation path is used by the API transport.
                service._resolve_upstream_for_payload = lambda _: {}
                service._apply_prompt_cache_hints = lambda *_: None
                _, ids, _ = await prepare(external=False)
                self.assertIn("ramble-fixture", ids)
                tool_history = payload["messages"] + [
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
                    {"role": "tool", "tool_call_id": "call-1", "content": "done"},
                ]
                _, ids, debug = await prepare(tool_history)
                self.assertEqual(debug["ramble_memory"], "")
                service.inject_total_budget = 0
                _, ids, debug = await prepare()
                self.assertNotIn("ramble-fixture", ids or [])
                self.assertEqual(debug["ramble_memory"], "")
            finally:
                await service.close()


class ManualBreathTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_breath_branch_topic_date_limits_alias_and_zero_budget(self):
        # Load the actual function without starting server background workers or loading config.
        from recall_policy import RecallPolicy
        from utils import parse_human_date_reference, strip_human_date_references
        tree = ast.parse(Path(__file__).with_name("server.py").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "breath")
        fn.decorator_list = []
        rows = [bucket("a", "海边散步的灯光", date="2026-06-15"),
                bucket("b", "海边散步的风声", date="2026-06-16"),
                bucket("c", "烘焙面包", date="2026-06-15")]
        ns = dict(
            decay_engine=SimpleNamespace(ensure_started=AsyncMock()), strip_raw_client_context=str,
            _int_between=lambda value, default, low, high: max(low, min(high, int(value))),
            _float_between=lambda value, default, low, high: max(low, min(high, float(value))),
            _bool_value=lambda value, default: bool(value), _normalize_direct_render_mode=str,
            _normalize_retrieval_mode=str, _normalize_breath_mode=str,
            parse_human_date_reference=parse_human_date_reference,
            _breath_query_requests_date_read=lambda query: bool(parse_human_date_reference(query)),
            _is_self_anchor_domain=lambda _: False, _is_pending_followup_domain=lambda _: False,
            _breath_query_requests_pending_followups=lambda _: False,
            bucket_mgr=SimpleNamespace(list_all=AsyncMock(return_value=rows)),
            _is_daily_impression_feel_bucket=lambda b: "daily_impression" in b["metadata"]["tags"],
            _bucket_matches_breath_date=lambda b, day: b["metadata"]["date"] == day,
            _strip_breath_date_query_shell=lambda query: strip_human_date_references(query).strip(),
            embedding_engine=None, _recall_policy=RecallPolicy,
            select_rambles=select_rambles, render_rambles=render_rambles,
        )
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "breath-fixture", "exec"), ns)
        breath = ns["breath"]
        text = await breath(domain="ramble", query="海边散步", max_results=1)
        self.assertEqual(len(re.findall(r"\[bucket_id:", text)), 1)
        self.assertNotIn("[bucket_id:c]", text)
        text = await breath(domain="whisper", query="2026-06-15")
        self.assertIn("[bucket_id:a]", text)
        self.assertIn("[bucket_id:c]", text)
        self.assertNotIn("[bucket_id:b]", text)
        text = await breath(domain="whisper", date="2026-06-15", query="海边散步")
        self.assertIn("[bucket_id:a]", text)
        self.assertNotIn("[bucket_id:c]", text)
        self.assertNotIn("[bucket_id:", await breath(domain="whisper", max_tokens=0))


if __name__ == "__main__":
    unittest.main()
