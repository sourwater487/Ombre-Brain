"""Exercise interval gates through real payload preparation, without network or live data."""
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gateway import GatewayService


async def verify():
    with TemporaryDirectory() as directory:
        service = GatewayService({
            "buckets_dir": directory, "state_dir": directory,
            "gateway": {"upstreams": [], "recalled_memory_interval_rounds": 2,
                        "related_memory_interval_rounds": 4},
            "persona": {"enabled": False}, "embedding": {"enabled": False},
        })
        try:
            for mode in ("task", "intimate", "playful", "memory_lookup", "conflict_repair", "reflective_repair"):
                assert service._build_injected_context_messages("", "", "", context_mode=mode) == ("", "")
                _, reminder = service._build_injected_context_messages(
                    "", "", "", context_mode=mode, active_reminders="REMINDER_FIXTURE")
                assert "REMINDER_FIXTURE" in reminder and "context_mode:" not in reminder
                stable, dynamic = service._build_injected_context_messages(
                    "", "CORE_FIXTURE", "", context_mode=mode)
                assert "CORE_FIXTURE" in stable and dynamic == ""
            assert service._gateway_memory_config_payload()["recalled_memory_interval_rounds"] == 2
            service.retrieval_mode = "graph"
            service._auto_recall_low_signal_query = lambda _: False
            service._route_memory_sentinel = AsyncMock(return_value={"route": "search"})
            service._route_domain_sentinel = AsyncMock(return_value={})
            service._domain_sentinel_should_skip_recall = lambda *_: False
            service._refresh_moment_graph = lambda _: ([], {}, [])
            moment = {"bucket_id": "direct", "moment_id": "direct:1",
                      "text": "technical fixture query", "metadata": {"bucket_name": "technical fixture query"}}
            select = service._select_dynamic_moments = AsyncMock(
                return_value=([moment], [moment], [], [], {}))
            render = service._format_recalled_moments = AsyncMock(return_value="DIRECT_FIXTURE")
            diffuse = service._build_moment_diffused_memory_with_debug = Mock(
                return_value=("[bucket_id:related] RELATED_FIXTURE", []))
            service._build_targeted_memory_detail = Mock(return_value=("", {}))
            service._build_injection_debug_payload = lambda **kwargs: kwargs

            async def prepare(session="a", external=True, messages=None):
                return await service.prepare_payload(
                    {"model": "test", "messages": messages or [
                        {"role": "user", "content": "technical fixture query"}]},
                    session, include_debug=True, external_transport=external)

            # First-turn injection and existing modulo cadence: 1, 2, 4, 6, 8 / 1, 4, 8.
            for next_round in range(1, 9):
                select.reset_mock(); render.reset_mock(); diffuse.reset_mock()
                prepared, ids, debug = await prepare()
                direct_due = next_round == 1 or next_round % 2 == 0
                related_due = next_round == 1 or next_round % 4 == 0
                assert bool(render.await_count) == direct_due, next_round
                assert bool(diffuse.call_count) == related_due, next_round
                assert bool(select.await_count) == (direct_due or related_due), next_round
                assert ("direct" in ids) == direct_due, (next_round, ids)
                assert ("related" in ids) == related_due, (next_round, ids)
                assert debug["recall_interval_debug"]["next_round"] == next_round
                # Classification stays available internally, never in the model context.
                assert debug["context_mode"] == "task"
                assert "context_mode:" not in str(prepared["messages"])
                assert "Context Mode" not in debug["dynamic_context"]
                if not direct_due and not related_due:
                    assert debug["dynamic_context"] == ""
                    assert prepared["messages"] == [{"role": "user", "content": "technical fixture query"}]
                else:
                    assert service._memory_reading_policy_context() in debug["dynamic_context"]
                assert service.state_store.get_current_round("a") == next_round - 1
                # Preparing/retrying alone never advances the successful-round counter.
                _, retry_ids, _ = await prepare()
                assert retry_ids == ids
                service.state_store.record_success("a", ids)

            # Other sessions retain their own first-turn schedule.
            _, ids, debug = await prepare("b")
            assert set(ids) == {"direct", "related"}
            assert debug["recall_interval_debug"]["next_round"] == 1

            # Independent intervals must not mark diffusion-only seeds as injected.
            service._apply_gateway_memory_config({"recalled_memory_interval_rounds": 3,
                                                  "related_memory_interval_rounds": 2})
            service.state_store.record_success("b", [])
            _, ids, debug = await prepare("b")
            assert ids == ["related"] and debug["recalled_memory"] == ""
            assert debug["recalled_moments"] == []

            # API and external/CC transports use the same gates.
            service._resolve_upstream_for_payload = lambda _: {}
            service._apply_prompt_cache_hints = lambda *_: None
            _, api_ids, _ = await prepare("b", external=False)
            assert api_ids == ids

            # Zero disables automatic recall; one restores every-round behavior.
            for interval in (0, 1):
                service._apply_gateway_memory_config({"recalled_memory_interval_rounds": interval,
                                                      "related_memory_interval_rounds": interval})
                _, ids, debug = await prepare()
                assert bool(ids) == bool(interval)
                assert service.gateway_cfg["recalled_memory_interval_rounds"] == interval
            service._apply_gateway_memory_config({"recalled_memory_interval_rounds": -1,
                                                  "related_memory_interval_rounds": 0})
            assert service.recalled_memory_interval_rounds == 0
            # Explicit targeted lookup is not part of periodic broad recall.
            service._build_targeted_memory_detail = Mock(
                return_value=("TARGETED_FIXTURE", {"accepted_ids": ["targeted"]}))
            _, ids, debug = await prepare()
            assert "targeted" in ids and "TARGETED_FIXTURE" in debug["dynamic_context"]

            # Tool continuation does not receive another periodic injection.
            _, ids, debug = await prepare(messages=[
                {"role": "user", "content": "technical fixture query"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "call-1", "type": "function", "function": {"name": "test", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call-1", "content": "done"},
            ])
            assert not debug["recall_interval_debug"]["direct_recall_due"]
            assert not debug["recall_interval_debug"]["related_recall_due"]
        finally:
            await service.close()
    print("Recall intervals: full preparation, independent cadence, config updates, session isolation, retries and tool continuation passed")


if __name__ == "__main__":
    asyncio.run(verify())
