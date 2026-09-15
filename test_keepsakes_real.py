import asyncio
import base64
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, '/app')
from bucket_manager import BucketManager
from keepsakes import Keepsakes, register_keepsakes
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
import httpx

async def main():
    with tempfile.TemporaryDirectory(prefix='keepsakes-test-') as root:
        manager = BucketManager({'buckets_dir': root})
        async def delete(key):
            if not await manager.get(key): return {'status': 'not_found'}
            return {'status': 'deleted' if await manager.delete(key) else 'failed'}
        service = Keepsakes(root, manager, delete, lambda key: ({}, []), lambda key: None)
        routes = []
        class Registrar:
            def custom_route(self, path, methods):
                def decorate(endpoint):
                    routes.append(Route(path, endpoint, methods=methods))
                    return endpoint
                return decorate
        def auth(request):
            return None if request.headers.get('authorization') == 'Bearer test-only' else JSONResponse({'error': 'unauthorized'}, status_code=401)
        register_keepsakes(Registrar(), service, auth)
        app = Starlette(routes=routes)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/api/keepsakes')).status_code == 401
            client.headers['authorization'] = 'Bearer test-only'
            body = {'turnId': 'chat:a:1', 'conversationId': 'a', 'cards': [
                {'type': 'ramble', 'title': '自由填写的标题', 'content': '我想留下这一句话。', 'valence': .8, 'arousal': .2},
                {'type': 'note', 'content': '我还在这里。', 'valence': .6, 'arousal': .4}]}
            created = await client.post('/api/keepsakes', json=body)
            assert created.status_code == 200, created.text
            cards = created.json()['cards']; assert len(cards) == 2
            assert len(list(Path(root).rglob('*.md'))) == 2
            bucket = await manager.get(cards[0]['id'])
            assert bucket['metadata']['name'] == '自由填写的标题'
            assert bucket['metadata']['tags'] == ['whisper'] and bucket['metadata']['type'] == 'feel'
            assert bucket['metadata']['valence'] == .8 and bucket['metadata']['arousal'] == .2
            assert bucket['metadata']['created'] == cards[0]['createdAt']
            assert 'valence' not in cards[0] and 'arousal' not in cards[0]
            response = await client.post('/api/keepsakes', json=body)
            assert response.json()['cards'] == cards
            assert len(list(Path(root).rglob('*.md'))) == 2
            assert (await client.post('/api/keepsakes/bind', json={'turnId': 'chat:a:1', 'conversationId': 'a', 'messageId': '2'})).status_code == 200
            assert (await client.get('/api/keepsakes?conversationId=a')).json()['cards'][0]['messageId'] == '2'
            png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jO1kAAAAASUVORK5CYII='
            response = await client.patch('/api/keepsakes/' + cards[0]['id'], json={'backgroundUrl': 'data:image/png;base64,' + png, 'textColor': 'white'})
            assert response.status_code == 200, response.text
            appearance = response.json()['card']['appearance']
            assert appearance['backgroundUrl'].startswith('/api/cards/')
            response = await client.patch('/api/keepsakes/' + cards[0]['id'], json={**appearance, 'textColor': 'black'})
            assert response.status_code == 200, response.text
            image = await client.get('/api/keepsakes/' + cards[0]['id'] + '/background')
            assert image.content == base64.b64decode(png)
            assert (await client.request('DELETE', '/api/keepsakes/' + cards[0]['id'], json={})).status_code == 400
            for index, card in enumerate(cards):
                response = await client.request('DELETE', '/api/keepsakes/' + card['id'], json={'confirm': 'DELETE'})
                assert response.status_code == 200, response.text
                assert await manager.get(card['id']) is None
                assert len((await client.get('/api/keepsakes')).json()['cards']) == 1 - index
            assert (await client.post('/api/keepsakes', json=body)).json()['cards'] == []
            assert len(list(Path(root).rglob('*.md'))) == 0
            print('Real BucketManager + HTTP: auth, feel/whisper, free title, affect, timestamp, idempotency, binding, uploaded background, confirmed deletion and no resurrection passed.')

asyncio.run(main())
