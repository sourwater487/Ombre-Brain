import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
from diary_recall import recall_diaries, topic_terms, DIARY_RERANK_THRESHOLD
from recall_policy import RecallPolicy
from utils import count_tokens_approx

TEXT = '黄山旅行时，我们一起在山顶看了日出，也拍了许多照片。'
ROW = {'id': 7, 'date': '2026-10-01', 'author': 'user', 'content': TEXT}

class DiaryRecallTests(unittest.IsolatedAsyncioTestCase):
    async def run_recall(self, score=.9, rows=None, messages=None, fresh=None, budget=500, broken=False):
        rows = [ROW] if rows is None else rows
        def handler(request):
            if broken: return httpx.Response(503)
            if request.method == 'POST': return httpx.Response(200, json={'diaries': rows})
            if fresh == 'deleted': return httpx.Response(404)
            return httpx.Response(200, json=fresh or next(r for r in rows if str(r['id']) == request.url.path.split('/')[-1]))
        reranker = SimpleNamespace(enabled=True, rerank=AsyncMock(return_value=[SimpleNamespace(index=i, score=score) for i in range(len(rows))]))
        policy = SimpleNamespace(specific_query_terms=lambda q: ['黄山', '旅行'])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await recall_diaries('黄山旅行时看日出的事', policy=policy, reranker=reranker,
                                          messages=messages or [], budget=budget, client=client)
        return result, reranker

    async def test_threshold_boundary(self):
        self.assertEqual(DIARY_RERANK_THRESHOLD, .9)
        for score in [.8, .899999, float('nan'), float('inf')]:
            (text, _), _ = await self.run_recall(score=score)
            self.assertEqual(text, '')
        (text, debug), reranker = await self.run_recall()
        self.assertIn(TEXT, text)
        self.assertIn('/diary?entry=7', text)
        self.assertEqual(debug['selected'][0]['score'], .9)
        self.assertEqual(reranker.rerank.call_args.args[1], [TEXT])

    async def test_no_emotion_only_or_generic_topic(self):
        self.assertEqual(topic_terms('难过', SimpleNamespace(specific_query_terms=lambda q: ['难过','日记'])), [])
        self.assertTrue(topic_terms('黄山旅行看日出', RecallPolicy()))
        (text, _), reranker = await self.run_recall(rows=[{**ROW, 'content': '今天有一点难过，想起过去许多日子。'}])
        self.assertEqual(text, '')
        reranker.rerank.assert_not_called()

    async def test_visible_and_freshness(self):
        for messages, fresh in [([{'content': TEXT}], None), ([], 'deleted'), ([], {**ROW, 'content': '已经修改成新的原文'})]:
            (text, _), _ = await self.run_recall(messages=messages, fresh=fresh)
            self.assertEqual(text, '')

    async def test_budget_outage_and_limit(self):
        (text, _), _ = await self.run_recall(budget=1)
        self.assertEqual(text, '')
        (text, debug), _ = await self.run_recall(broken=True)
        self.assertEqual(text, '')
        self.assertEqual(debug['reason'], 'retrieval_unavailable')
        (text, debug), _ = await self.run_recall(rows=[{**ROW, 'id': i} for i in [1,2,3]])
        self.assertEqual(len(debug['selected']), 2)
        self.assertLessEqual(count_tokens_approx(text), 500)


class GatewayDiaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_prepare_modes_interval_and_continuation(self):
        import tempfile
        from unittest.mock import patch
        from gateway import GatewayService
        with tempfile.TemporaryDirectory() as directory:
            service = GatewayService({'buckets_dir': directory, 'state_dir': directory,
                'gateway': {'upstreams': [], 'recalled_memory_budget': 500, 'ramble_memory_budget': 0,
                            'related_memory_budget': 0, 'core_memory_budget': 0, 'recent_context_budget': 0,
                            'inject_total_budget': 600},
                'persona': {'enabled': False}, 'embedding': {'enabled': False}})
            try:
                service._list_gateway_buckets = AsyncMock(return_value=[])
                service._auto_recall_low_signal_query = lambda _: False
                service._route_memory_sentinel = AsyncMock(return_value={'route': 'search'})
                service._route_domain_sentinel = AsyncMock(return_value={})
                service._domain_sentinel_should_skip_recall = lambda *_: False
                payload = {'model': 'test', 'messages': [{'role': 'user', 'content': '黄山旅行时的日出'}]}
                mock = AsyncMock(return_value=(TEXT, {'status': 'injected', 'threshold': .9}))
                with patch('gateway.recall_diaries', mock):
                    for external in [True, False]:
                        service._resolve_upstream_for_payload = lambda _: {}
                        service._apply_prompt_cache_hints = lambda *_: None
                        prepared, ids, debug = await service.prepare_payload(payload, 'a', include_debug=True, external_transport=external)
                        self.assertIn(TEXT, str(prepared['messages']))
                        self.assertEqual(debug['diary_recall_debug']['threshold'], .9)
                        self.assertLessEqual(mock.call_args.kwargs['budget'], 500)
                    mock.reset_mock()
                    service._should_inject_interval = lambda *_: False
                    await service.prepare_payload(payload, 'a', include_debug=True, external_transport=True)
                    mock.assert_not_called()
                    service._should_inject_interval = lambda *_: True
                    continuation = {**payload, 'messages': payload['messages'] + [
                        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 't'}]},
                        {'role': 'tool', 'tool_call_id': 't', 'content': 'done'}]}
                    await service.prepare_payload(continuation, 'a', include_debug=True, external_transport=True)
                    mock.assert_not_called()
            finally:
                await service.close()

if __name__ == '__main__': unittest.main()
