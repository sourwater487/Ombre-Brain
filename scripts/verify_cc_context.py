import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from starlette.requests import Request
from gateway import GatewayService

def request(action, body, session="a", auth="Bearer test"):
    data = json.dumps(body).encode()
    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}
    return Request({"type": "http", "method": "POST", "scheme": "http", "server": ("test",80),
                    "path": "/api/context/"+action, "query_string": b"",
                    "headers": [(b"authorization",auth.encode()), (b"x-ombre-session-id",session.encode())]}, receive)

async def main():
    service = object.__new__(GatewayService)
    service.gateway_token = "test"
    # Keep real preparation contract and successful-round dispatch, mock memory I/O.
    service._authorize = lambda auth: None if auth == "Bearer test" else __import__('starlette.responses',fromlist=['JSONResponse']).JSONResponse({},status_code=401)
    service.prepare_payload = AsyncMock(return_value=({"messages": [{"role":"system","content":"memory"},{"role":"user","content":"hello"}]}, ["bucket"], {"stable_context":"memory"}))
    service._record_successful_round = AsyncMock()
    service._update_persona_after_assistant_message = AsyncMock()
    response = await service.handle_external_context(request("prepare", {"messages":[{"role":"user","content":"hello"}],"model":"cc"}))
    assert response.status_code == 200
    result = json.loads(response.body)
    assert result["messages"][0]["content"] == "memory"
    assert service.prepare_payload.call_args.kwargs["external_transport"] is True
    assert service._record_successful_round.await_count == 0
    completion = {"ticket":result["ticket"],"assistant_message":{"content":"reply"}}
    assert (await service.handle_external_context(request("complete",completion,session="b"))).status_code == 409
    assert (await service.handle_external_context(request("complete",completion,auth="bad"))).status_code == 401
    assert (await service.handle_external_context(request("complete",completion))).status_code == 200
    assert (await service.handle_external_context(request("complete",completion))).status_code == 409
    assert service._record_successful_round.await_count == 1
    assert service._record_successful_round.call_args.args[:2] == ('a', ['bucket'])
    assert (await service.handle_external_context(request("prepare", {"messages":[1]}))).status_code == 400
    response = await service.handle_external_context(request("prepare", {"messages":[{"role":"user","content":"hello"}],"request_mode":"keepalive_autonomous"}))
    assert service.prepare_payload.call_args.kwargs['request_mode'] == 'keepalive_autonomous'
    ticket = json.loads(response.body)['ticket']
    service._external_context_turns[ticket]['expires'] = 0
    assert (await service.handle_external_context(request('complete',{'ticket':ticket,'assistant_message':{'content':'late'}}))).status_code == 409
    assert service._update_persona_after_assistant_message.await_count == 1
    # Exercise the real full pipeline without API routing, network or live memory.
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as directory:
        real = GatewayService({"buckets_dir": directory, "gateway": {"upstreams": []},
                               "persona": {"enabled": False}, "embedding": {"enabled": False}})
        real._resolve_upstream_for_payload = lambda *_: (_ for _ in ()).throw(AssertionError("API route used"))
        real._apply_prompt_cache_hints = lambda *_: (_ for _ in ()).throw(AssertionError("API cache used"))
        prepared, recalled, debug = await real.prepare_payload(
            {"model":"cc-subscription", "messages":[{"role":"user","content":"hello"}]},
            "isolated-cc", include_debug=True, external_transport=True)
        assert prepared['messages'] and isinstance(debug, dict)
        assert real.state_store.get_current_round('isolated-cc') == 0
        await real.close()
    print('CC context contracts and full preparation passed')

asyncio.run(main())
