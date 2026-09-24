import asyncio
from copy import deepcopy
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from gateway import GatewayService, OMBRE_UPSTREAM_NAME_FIELD, OMBRE_UPSTREAM_API_KEY_FIELD

async def verify():
    service = object.__new__(GatewayService)
    service.upstreams = [{'name': 'openrouter', 'base_url': 'https://openrouter.ai/api/v1', 'api_keys': [{'value': 'configured-key', 'label': 'test'}], 'allow_request_api_key': True, 'model_map': {}}]
    service.upstream_key_cooldown_seconds = 0
    service.upstream_key_cooldowns = {}
    service.upstream_default_model = 'claude-test'
    body = {
        'model': 'claude-test', 'prompt_cache_key': 'session', 'prompt_cache_retention': '1h', 'prompt_cache_ttl': '5m',
        'provider': {'only': ['anthropic']}, 'reasoning': {'enabled': True},
        OMBRE_UPSTREAM_NAME_FIELD: 'openai:https://relay.example/v1/chat/completions',
        OMBRE_UPSTREAM_API_KEY_FIELD: 'profile-test-key',
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hello', 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}]},
                     {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 'test', 'arguments': '{}'}}]},
                     {'role': 'tool', 'name': 'test', 'tool_call_id': 'c1', 'content': 'result'}],
        'tools': [{'type': 'function', 'cache_control': {'type': 'ephemeral'}, 'function': {'name': 'test', 'parameters': {'type': 'object', 'properties': {'cache_control': {'type': 'string'}}}}}],
    }
    snapshot = deepcopy(body)
    requests = []
    def handler(request):
        requests.append(request)
        actual = json.loads(request.content)
        assert str(request.url) == 'https://relay.example/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer profile-test-key'
        for field in ('prompt_cache_key', 'prompt_cache_retention', 'prompt_cache_ttl', 'provider', 'reasoning', OMBRE_UPSTREAM_NAME_FIELD, OMBRE_UPSTREAM_API_KEY_FIELD):
            assert field not in actual
        assert 'cache_control' not in actual['messages'][0]['content'][0]
        assert 'cache_control' not in actual['tools'][0]
        assert actual['tools'][0]['function']['parameters'] == body['tools'][0]['function']['parameters']
        assert actual['messages'][1] == body['messages'][1]
        assert actual['messages'][2] == {'role': 'tool', 'tool_call_id': 'c1', 'content': 'result'}
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service.http_client = client
        assert (await service._forward_upstream(body)).status_code == 200
        route = service._resolve_upstream_for_payload(body)
        response = await service._open_upstream_stream(route, {**body, 'stream': True})
        assert response.status_code == 200
        await response.aclose()
    assert len(requests) == 2
    assert body == snapshot
    assert service.upstreams[0]['api_keys'][0]['value'] == 'configured-key'
    or_route = {'upstream': service.upstreams[0], 'upstream_model': body['model']}
    preserved = service._openai_payload_for_upstream(body, or_route)
    assert preserved['messages'] == body['messages']
    assert preserved['prompt_cache_retention'] == '1h'
    assert preserved['provider'] == body['provider']
    try:
        service._resolve_upstream_for_payload({**body, OMBRE_UPSTREAM_API_KEY_FIELD: ''})
        raise AssertionError('Missing profile key accepted')
    except ValueError:
        pass
    print('Generic relay mock HTTP checks passed: stream, nonstream, tool continuation, cache removal, OR preservation, request isolation.')

asyncio.run(verify())
