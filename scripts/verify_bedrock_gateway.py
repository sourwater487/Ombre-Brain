"""Offline AWS routing/cache contracts; synthetic inputs only, no config loading."""
import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from gateway import GatewayService, OMBRE_UPSTREAM_NAME_FIELD, OMBRE_UPSTREAM_API_KEY_FIELD

service = object.__new__(GatewayService)
service.gateway_cfg = {"anthropic_max_tokens": 8192}
service.upstreams = [{"name": "or", "base_url": "https://openrouter.ai/api/v1", "protocol": "openai",
                      "api_keys": [{"value": "synthetic-or-key", "label": "or"}], "allow_request_api_key": True,
                      "model_map": {}, "prompt_cache": ""}]
service.upstream_default_model = "or-default"
service.upstream_key_cooldown_seconds = 0
service.upstream_key_cooldowns = {}
fixture = json.loads(Path(sys.argv[1]).read_text())
payload = fixture["body"]
payload[OMBRE_UPSTREAM_NAME_FIELD] = fixture["headers"]["X-Ombre-Upstream-Name"]
payload[OMBRE_UPSTREAM_API_KEY_FIELD] = fixture["headers"]["X-Ombre-Upstream-Api-Key"]
original_upstreams = deepcopy(service.upstreams)
route = service._resolve_upstream_for_payload(payload)
assert route["upstream"]["protocol"] == "anthropic"
assert route["upstream"]["base_url"] == "https://bedrock-runtime.us-east-1.amazonaws.com/anthropic/v1"
assert route["upstream"]["api_keys"][0]["value"] == "synthetic-aws-key"
assert service.upstreams == original_upstreams
assert service._resolve_upstream_for_payload({"model": "or-model"})["upstream"]["name"] == "or"
for invalid in ["https://evil.test/anthropic/v1", "https://bedrock-runtime.us-east-1.amazonaws.com.evil.test/anthropic/v1",
                "http://bedrock-runtime.us-east-1.amazonaws.com/anthropic/v1", "https://bedrock-runtime.us-east-1.amazonaws.com:8443/anthropic/v1",
                "https://user:pass@bedrock-runtime.us-east-1.amazonaws.com/anthropic/v1",
                "https://bedrock-runtime.us-east-1.amazonaws.com/anthropic/v1?key=x"]:
    try:
        service._trusted_request_upstream("bedrock:" + invalid, payload["model"])
    except ValueError:
        pass
    else:
        raise AssertionError("Accepted invalid AWS target")
try:
    service._resolve_upstream_for_payload({**payload, OMBRE_UPSTREAM_API_KEY_FIELD: ""})
except ValueError:
    pass
else:
    raise AssertionError("AWS must not fall back to OR credentials")

# Exercise the actual injection and conversion functions, retaining the four
# website-owned cache points through both initial and tool continuation calls.
first = deepcopy(payload)
first["messages"] = service._inject_context_messages(first["messages"], "Synthetic stable memory", "Synthetic live memory")
converted = service._anthropic_payload_for_upstream(first, route)
plan = service._anthropic_cache_control_plan(converted)
assert len(plan) == 4, plan
assert all(item["ttl"] == "1h" and item["location"].startswith("messages[") for item in plan), plan
assert "Synthetic live memory" in json.dumps(converted["messages"][-1])
assert "cache_control" not in json.dumps(converted["messages"][-1])
assert "__ombre" not in json.dumps(converted)
assert "provider" not in converted and "stream_options" not in converted
assert converted["max_tokens"] == 8192
assert converted["thinking"]["type"] == "adaptive"
assert converted["output_config"] == {"effort": "high"}
second = deepcopy(first)
second["messages"] += [
    {"role": "assistant", "content": "", "reasoning_details": [
        {"type": "thinking", "index": 0, "thinking": "Actual synthetic thought", "signature": "signed"}],
     "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call-1", "content": "Synthetic tool result"}]
second["messages"] = service._inject_context_messages(second["messages"], "Changed memory", "Changed dynamic memory")
continued = service._anthropic_payload_for_upstream(second, route)
assert service._anthropic_cache_control_plan(continued) == plan
assert continued.get("system") == converted.get("system")
assert continued["messages"][:5] == converted["messages"][:5], "Stable prefix changed during tools"
assert continued["messages"][-2]["content"][0]["signature"] == "signed"
assert continued["messages"][-1]["content"][0]["tool_use_id"] == "call-1"
# A caller disabling explicit caching must not receive gateway-generated points.
uncached = deepcopy(first)
for message in uncached["messages"]:
    if isinstance(message.get("content"), list):
        for block in message["content"]:
            block.pop("cache_control", None)
assert service._anthropic_cache_control_plan(service._anthropic_payload_for_upstream(uncached, route)) == []
# System checkpoints preserve their content block boundary too.
system_case = {**payload, "messages": [{"role": "system", "content": [
    {"type": "text", "text": "Stable system", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]},
    {"role": "user", "content": "Question"}]}
assert service._anthropic_payload_for_upstream(system_case, route)["system"][0]["cache_control"]["ttl"] == "1h"

async def verify_transport():
    async def handle(request):
        assert str(request.url) == "https://bedrock-runtime.us-east-1.amazonaws.com/anthropic/v1/messages"
        assert request.headers["x-api-key"] == "synthetic-aws-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        sent = json.loads(request.content)
        assert len(service._anthropic_cache_control_plan(sent)) == 4
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 15, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 1200, "output_tokens": 22}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service.http_client = client
        response = await service._forward_anthropic_upstream(first, route)
        usage = service._anthropic_usage_to_openai_usage(response.json()["usage"])
        assert usage["prompt_tokens"] == 6215 and usage["total_tokens"] == 6237

asyncio.run(verify_transport())
print("Bedrock gateway: trusted route, unchanged OR, four client breakpoints, memory injection, tool continuation and transport passed.")
