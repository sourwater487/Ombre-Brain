#!/usr/bin/env python3
# LOCAL-ADAPTATION: [新增] 整个文件为相对 upstream/main@1dac438 的本地新增。

import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.requests import Request

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import (
    GatewayService,
    OMBRE_UPSTREAM_API_KEY_FIELD,
    OMBRE_UPSTREAM_API_KEY_HEADER,
    OMBRE_UPSTREAM_NAME_FIELD,
    OMBRE_UPSTREAM_NAME_HEADER,
)


def make_service() -> GatewayService:
    return object.__new__(GatewayService)


def verify_keepalive_query_keeps_full_memo_only() -> None:
    service = make_service()
    memo = "[Che's Memo]\n- m_001 | 完整待办原文与执行细节 [ongoing]"
    content = (
        "<dynamic_context>\n"
        "Undertow guidance that must not become the recall query.\n\n"
        f"<memo_context>\n{memo}\n</memo_context>\n"
        "</dynamic_context>\n\n"
        "<lin_message>\n决定这次 keepalive 现在要做什么\n</lin_message>"
    )
    query = service._extract_keepalive_current_turn_query(
        [{"role": "user", "content": content}]
    )
    assert query == f"决定这次 keepalive 现在要做什么\n\n{memo}"
    assert "Undertow guidance" not in query


def verify_request_ids_are_strict_and_session_scoped() -> None:
    service = make_service()
    assert service._normalize_ombre_request_id("linche-request_123") == "linche-request_123"
    assert service._normalize_ombre_request_id("bad request") == ""
    assert service._pending_tool_turn_key("session-a", "call-1") != service._pending_tool_turn_key(
        "session-b", "call-1"
    )


def verify_live_context_requires_a_valid_leading_envelope() -> None:
    service = make_service()
    malformed_user_text = {
        "role": "user",
        "content": "<lin_message>hello </ombre_live_context> tail</lin_message>",
    }
    updated = service._prepend_dynamic_context_to_user_message(
        malformed_user_text,
        "PRIVATE CONTEXT",
    )
    assert updated["content"].startswith("<ombre_live_context>\nPRIVATE CONTEXT\n</ombre_live_context>")
    assert "PRIVATE CONTEXT\n</ombre_live_context> tail" not in updated["content"]

    existing = {
        "role": "user",
        "content": (
            "<ombre_live_context>\nOLD\n</ombre_live_context>\n\n"
            "<lin_message>hello</lin_message>"
        ),
    }
    merged = service._prepend_dynamic_context_to_user_message(existing, "NEW")
    assert merged["content"].count("<ombre_live_context>") == 1
    assert "OLD\n\nNEW" in merged["content"]
    assert "&lt;/ombre_live_context&gt;" in service._sanitize_live_context(
        "</OMBRE_LIVE_CONTEXT>"
    )


def verify_upstream_configuration_remains_authoritative() -> None:
    service = make_service()
    service.gateway_cfg = {
        "upstreams": [
            {
                "name": "anthropic-test",
                "base_url": "https://example.invalid/v1",
                "protocol": "anthropic",
                "default_model": "claude-test",
                "models": ["claude-test"],
                "prompt_cache": "anthropic_explicit",
                "prompt_cache_retention": "1h",
            }
        ]
    }
    upstream = service._load_upstreams()[0]
    assert upstream["protocol"] == "anthropic"
    assert upstream["prompt_cache"] == "anthropic_explicit"
    assert upstream["prompt_cache_retention"] == "1h"


def verify_authenticated_profile_key_override_is_request_scoped() -> None:
    service = make_service()
    service.gateway_cfg = {
        "upstreams": [
            {
                "name": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "protocol": "openai",
                "api_key": "configured-openrouter-key",
                "models": ["anthropic/claude-opus-4.6"],
            },
            {
                "name": "linkapi-claude",
                "base_url": "https://linkapi.ai/v1",
                "protocol": "anthropic",
                "api_key": "configured-linkapi-key",
                "models": ["claude-sonnet-5"],
                # Simulate a stale dashboard/runtime entry saved with Link 1h.
                # Effective routing must normalize it to the current 5m policy
                # without mutating the persisted config object.
                "prompt_cache_retention": "1h",
            },
        ]
    }
    service.upstreams = service._load_upstreams()
    service.upstream_default_model = "anthropic/claude-opus-4.6"
    assert service.upstreams[0]["prompt_cache"] == ""
    assert service.upstreams[1]["prompt_cache"] == "anthropic_explicit"
    assert service.upstreams[1]["prompt_cache_retention"] == "5m"
    assert "prompt_cache" not in service.gateway_cfg["upstreams"][1]
    assert service.gateway_cfg["upstreams"][1]["prompt_cache_retention"] == "1h"

    payload = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hello"}],
        OMBRE_UPSTREAM_API_KEY_FIELD: "profile-linkapi-key",
        OMBRE_UPSTREAM_NAME_FIELD: "linkapi-claude",
    }
    route = service._resolve_upstream_for_payload(payload)
    assert route["upstream"]["name"] == "linkapi-claude"
    assert route["upstream"]["protocol"] == "anthropic"
    assert route["upstream"]["allow_request_api_key"] is True
    assert route["upstream"]["api_keys"] == [
        {"value": "profile-linkapi-key", "label": "request:profile"}
    ]
    assert service.upstreams[1]["api_key"] == "configured-linkapi-key"

    upstream_payload = service._payload_for_upstream_model(payload, route["upstream_model"])
    assert OMBRE_UPSTREAM_API_KEY_FIELD not in upstream_payload
    assert OMBRE_UPSTREAM_NAME_FIELD not in upstream_payload
    assert upstream_payload["model"] == "claude-sonnet-5"
    gateway_source = (ROOT / "gateway.py").read_text(encoding="utf-8")
    assert OMBRE_UPSTREAM_API_KEY_HEADER in gateway_source
    assert OMBRE_UPSTREAM_NAME_HEADER in gateway_source

    conflicting_model_route = service._resolve_upstream_for_payload(
        {
            "model": "anthropic/claude-opus-4.6",
            OMBRE_UPSTREAM_NAME_FIELD: "linkapi-claude",
            OMBRE_UPSTREAM_API_KEY_FIELD: "profile-linkapi-key",
        }
    )
    assert conflicting_model_route["upstream"]["name"] == "linkapi-claude"
    assert conflicting_model_route["upstream_model"] == "anthropic/claude-opus-4.6"

    service.upstreams = service.upstreams[:1]
    fallback_route = service._resolve_upstream_for_payload(
        {
            "model": "claude-sonnet-5",
            OMBRE_UPSTREAM_NAME_FIELD: "linkapi-claude",
            OMBRE_UPSTREAM_API_KEY_FIELD: "profile-linkapi-key",
        }
    )
    assert fallback_route["upstream"]["name"] == "linkapi-claude"
    assert fallback_route["upstream"]["base_url"] == "https://linkapi.ai/v1"
    assert fallback_route["upstream"]["protocol"] == "anthropic"
    assert [upstream["name"] for upstream in service.upstreams] == ["openrouter"]


def verify_native_anthropic_thinking_and_cache_contracts() -> None:
    service = make_service()
    service.gateway_cfg = {"anthropic_max_tokens": 8192}
    upstream = {
        "name": "linkapi-claude",
        "protocol": "anthropic",
        "base_url": "https://linkapi.ai/v1",
        "prompt_cache": "anthropic_explicit",
        "prompt_cache_retention": "5m",
    }
    route = {
        "upstream": upstream,
        "upstream_model": "claude-opus-4-6",
    }
    payload = {
        "model": "claude-opus-4-6",
        "reasoning": {"enabled": True, "max_tokens": 4096, "effort": "max"},
        "messages": [
            {"role": "system", "content": "stable system"},
            {
                "role": "assistant",
                "content": "I will inspect it.",
                "reasoning_details": [
                    {"type": "thinking", "index": 0, "thinking": "Need "},
                    {"type": "thinking", "index": 0, "thinking": "context."},
                    {"type": "thinking", "index": 0, "signature": "opaque-signature"},
                ],
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "journal_read", "arguments": '{"id":1}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "journal_read",
                    "description": "Read a journal entry.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": "auto",
    }

    converted = service._anthropic_payload_for_upstream(payload, route)
    assert converted["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert converted["output_config"] == {"effort": "max"}
    assert service._anthropic_thinking_config(
        {"reasoning": {"enabled": True, "max_tokens": 4096}},
        max_tokens=8192,
        model="claude-opus-4-5",
    ) == {
        "type": "enabled",
        "budget_tokens": 4096,
        "display": "summarized",
    }
    assert converted["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert converted["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assistant_blocks = converted["messages"][0]["content"]
    assert assistant_blocks[0] == {
        "type": "thinking",
        "thinking": "Need context.",
        "signature": "opaque-signature",
    }
    assert assistant_blocks[-1]["type"] == "tool_use"
    assert assistant_blocks[-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert service._anthropic_cache_control_plan(converted) == [
        {"location": "tools[0]", "type": "ephemeral", "ttl": "5m"},
        {"location": "system[0]", "type": "ephemeral", "ttl": "5m"},
        {
            "location": f"messages[0].content[{len(assistant_blocks) - 1}]",
            "type": "ephemeral",
            "ttl": "5m",
        },
    ]

    rolling_cache_payload = {
        "tools": [{"name": "stable_tool", "input_schema": {"type": "object"}}],
        "system": [{"type": "text", "text": "stable system"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "old question"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
            {"role": "user", "content": [{"type": "text", "text": "prior question"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "prior answer"}]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "<ombre_live_context>dynamic recall</ombre_live_context>\ncurrent",
                    }
                ],
            },
        ],
    }
    service._apply_explicit_anthropic_cache_control(
        rolling_cache_payload,
        {"type": "ephemeral", "ttl": "5m"},
    )
    assert service._anthropic_cache_control_plan(rolling_cache_payload) == [
        {"location": "tools[0]", "type": "ephemeral", "ttl": "5m"},
        {"location": "system[0]", "type": "ephemeral", "ttl": "5m"},
        {"location": "messages[1].content[0]", "type": "ephemeral", "ttl": "5m"},
        {"location": "messages[3].content[0]", "type": "ephemeral", "ttl": "5m"},
    ]
    assert "cache_control" not in rolling_cache_payload["messages"][4]["content"][0]
    assert service._anthropic_live_context_locations(rolling_cache_payload) == [
        "messages[4].content[0]"
    ]

    tool_continuation_messages = service._inject_context_messages(
        [
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "inspect it"},
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {
                        "id": "call-tail",
                        "type": "function",
                        "function": {"name": "journal_read", "arguments": '{"id":2}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-tail", "content": "tool result"},
        ],
        "stable recalled context",
        "dynamic recalled context",
    )
    assert tool_continuation_messages[1]["role"] == "system"
    assert "<ombre_live_context>" in tool_continuation_messages[1]["content"]
    converted_tool_continuation = service._anthropic_payload_for_upstream(
        {
            "model": "claude-opus-4-6",
            "messages": tool_continuation_messages,
            "tools": payload["tools"],
            "tool_choice": "auto",
        },
        route,
    )
    assert "<ombre_live_context>" not in "".join(
        str(block.get("text") or "")
        for block in converted_tool_continuation["system"]
        if isinstance(block, dict)
    )
    tail_content = converted_tool_continuation["messages"][-1]["content"]
    assert any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and "<ombre_live_context>" in str(block.get("text") or "")
        for block in tail_content
    )
    assert all(
        "cache_control" not in block
        for block in tail_content
        if isinstance(block, dict)
    )
    assert service._anthropic_live_context_locations(converted_tool_continuation) == [
        f"messages[{len(converted_tool_continuation['messages']) - 1}].content[1]"
    ]

    replayed_tool_continuation = service._inject_context_messages(
        [
            {"role": "system", "content": "stable system"},
            {
                "role": "user",
                "content": (
                    "<ombre_live_context>\nfirst request recall\n</ombre_live_context>\n\n"
                    "<lin_message>inspect it</lin_message>"
                ),
            },
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {
                        "id": "call-replayed-tail",
                        "type": "function",
                        "function": {"name": "journal_read", "arguments": '{"id":2}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-replayed-tail",
                "content": "tool result",
            },
        ],
        "stable recalled context",
        "dynamic recalled context",
    )
    assert len(replayed_tool_continuation) == 5
    assert replayed_tool_continuation[1]["role"] == "system"
    assert replayed_tool_continuation[2]["content"].count("<ombre_live_context>") == 1
    converted_replayed_tool_continuation = service._anthropic_payload_for_upstream(
        {
            "model": "claude-opus-4-6",
            "messages": replayed_tool_continuation,
            "tools": payload["tools"],
            "tool_choice": "auto",
        },
        route,
    )
    assert service._anthropic_live_context_locations(converted_replayed_tool_continuation) == [
        "messages[0]"
    ]
    assert all(
        "<ombre_live_context>" not in str(block.get("text") or "")
        for block in converted_replayed_tool_continuation["messages"][-1]["content"]
        if isinstance(block, dict)
    )

    mixed_ttl_payload = {
        "tools": [
            {
                "name": "legacy_tool",
                "input_schema": {"type": "object"},
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ],
        "system": [
            {
                "type": "text",
                "text": "legacy system",
                "cache_control": {"type": "ephemeral", "ttl": "5m"},
            }
        ],
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "completed turn",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "current"}]},
        ],
    }
    service._apply_explicit_anthropic_cache_control(
        mixed_ttl_payload,
        {"type": "ephemeral", "ttl": "5m"},
    )
    normalized_plan = service._anthropic_cache_control_plan(mixed_ttl_payload)
    assert len(normalized_plan) == 3
    assert all(entry["ttl"] == "5m" for entry in normalized_plan)

    response_message = service._anthropic_response_body_to_openai_message(
        {
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Checking context.",
                    "signature": "response-signature",
                },
                {"type": "text", "text": "Done."},
            ]
        }
    )
    assert response_message["reasoning_content"] == "Checking context."
    assert response_message["reasoning_details"] == [
        {
            "type": "thinking",
            "index": 0,
            "thinking": "Checking context.",
            "signature": "response-signature",
        }
    ]

    first_usage = service._anthropic_usage_to_openai_usage(
        {
            "input_tokens": 512,
            "output_tokens": 190,
            "cache_creation_input_tokens": 116_799,
        }
    )
    assert first_usage["prompt_tokens"] == 117_311
    assert first_usage["completion_tokens"] == 190
    assert first_usage["total_tokens"] == 117_501
    assert first_usage["input_tokens"] == 512
    assert first_usage["cache_creation_input_tokens"] == 116_799

    second_usage = service._anthropic_usage_to_openai_usage(
        {
            "input_tokens": 622,
            "output_tokens": 233,
            "cache_read_input_tokens": 87_599,
            "cache_creation_input_tokens": 29_551,
            "output_tokens_details": {"thinking_tokens": 101},
        }
    )
    assert second_usage["prompt_tokens"] == 117_772
    assert second_usage["completion_tokens"] == 233
    assert second_usage["total_tokens"] == 118_005
    assert second_usage["prompt_tokens_details"] == {"cached_tokens": 87_599}
    assert second_usage["cache_creation_input_tokens"] == 29_551
    assert second_usage["completion_tokens_details"] == {"reasoning_tokens": 101}

    thinking_chunks = service._openai_chunks_from_anthropic_event(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "streamed thought"},
        },
        chunk_id="chatcmpl-test",
        created=1,
        model="claude-opus-4-6",
    )
    thinking_delta = thinking_chunks[0]["chunk"]["choices"][0]["delta"]
    assert thinking_delta["reasoning_content"] == "streamed thought"
    assert thinking_delta["reasoning_details"][0]["thinking"] == "streamed thought"

    signature_chunks = service._openai_chunks_from_anthropic_event(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "stream-signature"},
        },
        chunk_id="chatcmpl-test",
        created=1,
        model="claude-opus-4-6",
    )
    signature_delta = signature_chunks[0]["chunk"]["choices"][0]["delta"]
    assert signature_delta["thinking_signature"] == "stream-signature"
    assert signature_delta["reasoning_details"][0]["signature"] == "stream-signature"

    trusted = service._trusted_request_upstream("linkapi-claude", "claude-opus-4-6")
    assert trusted["prompt_cache"] == "anthropic_explicit"
    assert trusted["prompt_cache_retention"] == "5m"


def verify_embedding_hot_update_rebuilds_gateway_engine() -> None:
    with TemporaryDirectory() as tmp_dir:
        service = make_service()
        service.config = {
            "buckets_dir": tmp_dir,
            "dehydration": {
                "api_key": "dehydration-key",
                "base_url": "https://dehydration.example/v1",
            },
            "embedding": {
                "enabled": True,
                "model": "embedding-before",
                "base_url": "https://embedding-before.example/v1",
                "api_key": "embedding-key-before",
            },
        }
        service.embedding_cfg = service.config["embedding"]
        service.embedding_engine = None

        updated = service._apply_embedding_config(
            {
                "enabled": True,
                "model": "embedding-after",
                "base_url": "https://embedding-after.example/v1",
                "api_key": "embedding-key-after",
            }
        )

        assert updated == [
            "embedding.enabled",
            "embedding.model",
            "embedding.base_url",
            "embedding.api_key",
        ]
        assert service.embedding_engine.model == "embedding-after"
        assert service.embedding_engine.base_url == "https://embedding-after.example/v1"
        assert service.embedding_engine.api_key == "embedding-key-after"
        assert service._embedding_config_payload()["api_ready"] is True


def verify_dashboard_gateway_and_env_contracts() -> None:
    gateway_source = (ROOT / "gateway.py").read_text(encoding="utf-8")
    server_source = (ROOT / "server.py").read_text(encoding="utf-8")
    compose_source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'embedding_payload = body.get("embedding")' in gateway_source
    assert 'updated.extend(self._apply_embedding_config(embedding_payload))' in gateway_source
    assert 'gateway_hot_update_payload["embedding"] = embedding_gateway_payload' in server_source
    assert 'if gateway_hot_update_payload and not gateway_hot_update.get("ok", False):' in server_source
    assert '_expected_gateway_hot_update_paths(gateway_payload) - confirmed_updates' in server_source
    assert '"status": "gateway_hot_reload_incomplete"' in server_source
    assert '"rolled_back": True' in server_source
    assert 'sanitized["allow_request_api_key"] = _bool_value(' in server_source
    assert '"ok": False' in server_source
    assert "OMBRE_ENV_PATH: /app/.env" in compose_source
    assert "- ./.env:/app/.env" in compose_source


class DebugStateStore:
    def list_injection_debug(self, **_kwargs):
        return [
            {
                "id": 12,
                "payload": {
                    "request_id": "request-b",
                    "request_mode": "chat",
                    "dynamic_context": "B",
                },
            },
            {
                "id": 11,
                "payload": {
                    "request_id": "request-a",
                    "request_mode": "chat",
                    "dynamic_context": "A",
                },
            },
        ]


async def verify_debug_endpoint_filters_exact_request() -> None:
    service = make_service()
    service.gateway_token = "secret"
    service.state_store = DebugStateStore()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/debug/injections",
            "query_string": b"session_id=lin-main&request_id=request-a&limit=20",
            "headers": [(b"authorization", b"Bearer secret")],
        }
    )
    response = await service.handle_injection_debug(request)
    payload = json.loads(response.body)
    assert [item["id"] for item in payload["items"]] == [11]


def main() -> None:
    verify_keepalive_query_keeps_full_memo_only()
    verify_request_ids_are_strict_and_session_scoped()
    verify_live_context_requires_a_valid_leading_envelope()
    verify_upstream_configuration_remains_authoritative()
    verify_authenticated_profile_key_override_is_request_scoped()
    verify_native_anthropic_thinking_and_cache_contracts()
    verify_embedding_hot_update_rebuilds_gateway_engine()
    verify_dashboard_gateway_and_env_contracts()
    asyncio.run(verify_debug_endpoint_filters_exact_request())
    print("gateway integration contracts verification passed")


if __name__ == "__main__":
    main()
