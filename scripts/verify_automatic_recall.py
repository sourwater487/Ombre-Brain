"""Offline regression checks for incidental keyword recall and compact context."""
import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automatic_recall import select_automatic_fragment
from gateway import GatewayService
from memory_moments import parse_bucket_moments
from utils import count_tokens_approx


def make_bucket(text, title="申请材料进展"):
    return {"id": "fixture", "metadata": {"name": title, "date": "2026-09-09", "type": "dynamic"},
            "content": text}


async def verify():
    with TemporaryDirectory() as directory:
        service = GatewayService({"buckets_dir": directory, "state_dir": directory,
                                  "persona": {"enabled": False}, "embedding": {"enabled": False}})
        try:
            query = "羊肉粉哦，这家很好吃！我要去吃饭了。"
            bucket = make_bucket("晚上忙了很久的申请材料。晚饭吃了羊肉粉。明天继续等老师的回复。")
            moment = parse_bucket_moments(bucket)[0]
            moment.update(keyword_score=0.99, admission_reason="admitted_bucket")
            assert not service._prepare_automatic_recall_fragment(query, moment, bucket)
            assert moment["automatic_fragment_debug"]["semantic_score_present"] is False
            moment["semantic_score"] = 0.0
            assert not service._prepare_automatic_recall_fragment(query, moment, bucket)
            assert moment["automatic_fragment_debug"]["semantic_score_present"] is True

            # The same note remains accessible when explicitly asking for the event.
            explicit = "那天吃羊肉粉时我在忙什么？"
            assert service._prepare_automatic_recall_fragment(explicit, moment, bucket)
            rendered = await service._format_recalled_moments([moment], {}, [bucket], 1000, explicit)
            assert "申请材料" in rendered

            # A preference in a mixed record is useful; unrelated adjacent prose is not.
            preference = make_bucket("晚上忙了很久的申请材料。一直很喜欢这家羊肉粉，尤其喜欢清汤。明天等老师回复。")
            preferred = parse_bucket_moments(preference)[0]
            assert service._prepare_automatic_recall_fragment(query, preferred, preference)
            rendered = await service._format_recalled_moments([preferred], {}, [preference], 1000, query)
            assert "喜欢这家羊肉粉" in rendered and "申请材料" not in rendered and "老师" not in rendered
            assert "[bucket_id:fixture]" in rendered and "[date:2026-09-09]" in rendered
            assert "reading_note:" not in rendered
            assert await service._format_direct_bucket(preference, preferred, {}, 1, query_text=query) == ""
            wrong_moment = {**preferred, "text": "明天等老师回复。"}
            assert "[moment_id:" not in service._automatic_fragment_header(preference, wrong_moment)
            stable, dynamic = service._build_injected_context_messages("", "", "", recalled_memory=rendered)
            assert dynamic.count(service._memory_reading_policy_context()) == 1
            assert "Memory Reading Policy" not in dynamic and "Live private context" not in dynamic
            assert "Date Boundary" not in dynamic
            print("useful_preference_tokens", count_tokens_approx(dynamic))

            # Concrete technical memories remain useful without an explicit recall request.
            technical = make_bucket("Docker 网关连不上时，检查容器的端口映射。昨天还去逛了超市。", "Docker 网关连接排查")
            technical_moment = parse_bucket_moments(technical)[0]
            assert service._prepare_automatic_recall_fragment("Docker 网关连接不上", technical_moment, technical)
            assert "超市" not in technical_moment["_auto_recall_fragment"]
            # A missing score never automatically rejects lexical evidence or preference.
            assert preferred.get("semantic_score") is None
            # Semantic paraphrases can still pass when their actual moment has a strong score.
            semantic = {"bucket_id": "semantic", "text": "乘机前检查护照有效期。", "semantic_score": 0.95}
            assert service._prepare_automatic_recall_fragment("明天要坐飞机出国", semantic)
            assert "护照" in semantic["_auto_recall_fragment"]

            # Nested lexical tokens must not turn a single incidental word into two anchors.
            weak = select_automatic_fragment("晚饭吃了羊肉粉。", "材料", ["羊肉", "羊肉粉", "粉"])
            assert weak.fragment == ""
            # Keep complete sentences and negations rather than clipping a useful fragment.
            negated = select_automatic_fragment("不喜欢这家羊肉粉，汤太咸。", "饮食", ["羊肉粉"])
            assert negated.fragment.startswith("不喜欢")
            long = select_automatic_fragment("羊肉粉" + "很长的内容" * 100, "羊肉粉", ["羊肉粉"])
            assert long.fragment == ""

            # One common policy even if both stable and dynamic memory are present.
            stable, dynamic = service._build_injected_context_messages("", "core", "", recalled_memory=rendered)
            assert (stable + dynamic).count(service._memory_reading_policy_context()) == 1
            _, created = service._build_injected_context_messages("", "", "", recalled_memory="[created:2026-09-09] fixture")
            assert "Date Boundary" in created
            assert service._build_injected_context_messages("", "", "", context_mode="task") == ("", "")

            # Full preparation: an already admitted bucket must not bypass the final gate.
            service._list_gateway_buckets = AsyncMock(return_value=[bucket])
            service._auto_recall_low_signal_query = lambda _: False
            service._route_memory_sentinel = AsyncMock(return_value={"route": "search"})
            service._route_domain_sentinel = AsyncMock(return_value={})
            service._domain_sentinel_should_skip_recall = lambda *_: False
            candidate = parse_bucket_moments(bucket)[0]
            service._refresh_moment_graph = lambda _: ([candidate], {"fixture": [candidate]}, [])
            service._select_dynamic_moments = AsyncMock(return_value=([candidate], [candidate], [], [], {}))
            service._build_targeted_memory_detail = lambda *a, **kw: ("", {})
            prepared, ids, debug = await service.prepare_payload(
                {"model": "test", "messages": [{"role": "user", "content": query}]},
                "isolated", include_debug=True, external_transport=True)
            assert ids == [] and debug["dynamic_tokens"] == 0
            assert prepared["messages"] == [{"role": "user", "content": query}]
            assert "automatic_fragment_debug" not in candidate  # cached candidates stay unchanged
            assert any(row.get("admission_reason") == "automatic_fragment_not_useful"
                       for row in debug["suppressed_candidates"])
            print("incidental_keyword_tokens", debug["dynamic_tokens"])
        finally:
            await service.close()
    print("Automatic recall: relevance, explicit lookup, fragment rendering and compact policy passed")


if __name__ == "__main__":
    asyncio.run(verify())
