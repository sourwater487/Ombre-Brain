#!/usr/bin/env python3

import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.requests import Request

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import GatewayService


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
    verify_embedding_hot_update_rebuilds_gateway_engine()
    verify_dashboard_gateway_and_env_contracts()
    asyncio.run(verify_debug_endpoint_filters_exact_request())
    print("gateway integration contracts verification passed")


if __name__ == "__main__":
    main()
