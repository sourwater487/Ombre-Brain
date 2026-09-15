import copy
import tempfile
import unittest
from keepsakes import Keepsakes

CARD = {"type": "ramble", "title": "这一刻", "content": "想留下的话", "valence": .7, "arousal": .2}

class Buckets:
    def __init__(self):
        self.rows = {}
        self.fail = False
    async def create(self, **kw):
        if self.fail:
            raise OSError("disk full")
        self.rows[kw["bucket_id"]] = {"content": kw["content"], "metadata": {
            **kw["extra_metadata"], "created": kw["created"], "type": kw["bucket_type"],
            "name": kw["name"], "tags": kw["tags"], "valence": kw["valence"], "arousal": kw["arousal"]}}
    async def get(self, key):
        return self.rows.get(key)

class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.buckets = Buckets()
        self.fail_delete = False
        self.fail_cleanup = False
        async def delete(key):
            if self.fail_delete:
                return {"status": "failed"}
            self.buckets.rows.pop(key, None)
            return {"status": "deleted"}
        self.store = Keepsakes(self.temp.name, self.buckets, delete, lambda key: ({}, ["embedding"] if self.fail_cleanup else []), lambda key: None)
    async def asyncTearDown(self):
        self.temp.cleanup()
    def body(self, cards=None, turn="chat:a:1", conversation="a"):
        return {"conversationId": conversation, "turnId": turn, "cards": copy.deepcopy(cards or [CARD])}
    async def test_real_storage_and_metadata(self):
        cards = await self.store.create(self.body())
        row = self.buckets.rows[cards[0]["id"]]
        self.assertEqual(row["metadata"]["type"], "feel")
        self.assertEqual(row["metadata"]["tags"], ["whisper"])
        self.assertEqual(row["metadata"]["name"], CARD["title"])
        self.assertNotIn("valence", cards[0])
        self.assertNotIn("content", next(iter(self.store.read().values())))
    async def test_retry_and_restart_do_not_duplicate(self):
        first = await self.store.create(self.body())
        second = await self.store.create(self.body())
        self.assertEqual(first, second)
        self.assertEqual(len(self.buckets.rows), 1)
    async def test_batch_limits_and_next_turn(self):
        note = {**CARD, "type": "note", "title": ""}
        await self.store.create(self.body([CARD, note]))
        await self.store.create(self.body([CARD, note], turn="chat:a:2"))
        self.assertEqual(len(self.buckets.rows), 4)
        with self.assertRaises(ValueError):
            await self.store.create(self.body([{**CARD, "content": "other"}]))
        with self.assertRaises(ValueError):
            await self.store.create(self.body([CARD, CARD]))
    async def test_disk_failure_no_activity_and_retry_recovers(self):
        self.buckets.fail = True
        with self.assertRaises(OSError):
            await self.store.create(self.body())
        self.assertEqual(await self.store.list(), [])
        self.buckets.fail = False
        self.assertEqual(len(await self.store.create(self.body())), 1)
    async def test_ordinary_whisper_excluded_and_sessions_isolated(self):
        self.buckets.rows['ordinary'] = {"metadata": {"tags": ["whisper"]}, "content": "old memory"}
        await self.store.create(self.body())
        await self.store.create(self.body(conversation="b", turn="chat:b:2"))
        self.assertEqual(len(await self.store.list()), 2)
        self.assertEqual(len(await self.store.list(conversation_id="a")), 1)
    async def test_delete_confirmation_and_idempotency(self):
        cards = await self.store.create(self.body())
        key = cards[0]["id"]
        await self.store.bind('a', 'chat:a:1', '2')
        self.assertEqual((await self.store.list())[0]["messageId"], '2')
        with self.assertRaises(ValueError):
            await self.store.delete(key, None)
        await self.store.delete(key, 'DELETE')
        await self.store.delete(key, 'DELETE')
        self.assertEqual(await self.store.list(), [])
        self.assertEqual(await self.store.create(self.body()), [])
    async def test_delete_one_then_all_cards(self):
        note = {**CARD, "type": "note", "title": ""}
        cards = await self.store.create(self.body([CARD, note]))
        await self.store.delete(cards[0]['id'], 'DELETE')
        self.assertEqual(len(await self.store.list()), 1)
        await self.store.delete(cards[1]['id'], 'DELETE')
        self.assertEqual(await self.store.list(), [])
    async def test_failed_delete_keeps_card_and_cleanup_retry(self):
        key = (await self.store.create(self.body()))[0]['id']
        self.fail_delete = True
        with self.assertRaises(RuntimeError):
            await self.store.delete(key, 'DELETE')
        self.assertEqual(len(await self.store.list()), 1)
        self.fail_delete = False
        self.fail_cleanup = True
        with self.assertRaises(RuntimeError):
            await self.store.delete(key, 'DELETE')
        self.fail_cleanup = False
        await self.store.delete(key, 'DELETE')
        self.assertEqual(await self.store.list(), [])
    async def test_validation(self):
        for invalid in [{**CARD, 'valence': 3}, {**CARD, 'arousal': True}, {**CARD, 'type': 'diary'}, {**CARD, 'type': 'note'}, {**CARD, 'content': ''}]:
            with self.assertRaises(ValueError):
                await self.store.create(self.body([invalid]))
        self.assertEqual(await self.store.list(), [])
    async def test_appearance(self):
        key = (await self.store.create(self.body()))[0]['id']
        for invalid in ['javascript:alert(1)', 'data:image/svg+xml,<svg/>', 'file:///etc/passwd']:
            with self.assertRaises(ValueError):
                await self.store.appearance(key, {'backgroundUrl': invalid})
        card = await self.store.appearance(key, {'backgroundUrl': 'https://example.com/image.jpg', 'textColor': 'white'})
        self.assertEqual(card['appearance']['textColor'], 'white')

    async def test_preview_background_persists_and_rejects_non_http(self):
        key = (await self.store.create(self.body()))[0]['id']
        appearance = {'backgroundUrl': 'https://example.com/card.jpg', 'previewBackgroundUrl': 'https://example.com/bubble.jpg', 'textColor': 'white'}
        await self.store.appearance(key, appearance)
        self.assertEqual((await self.store.list())[0]['appearance'], appearance)
        for invalid in ['javascript:alert(1)', 'file:///etc/passwd', 'data:image/png;base64,YQ==', 123]:
            with self.assertRaises(ValueError):
                await self.store.appearance(key, {**appearance, 'previewBackgroundUrl': invalid})

    async def test_global_style_persists_without_mutating_cards(self):
        first = (await self.store.create(self.body()))[0]
        self.assertEqual(await self.store.global_appearance(), {})
        style = {'dayBackgroundUrl': 'https://example.com/day.jpg', 'nightBackgroundUrl': 'https://example.com/night.jpg', 'previewBackgroundUrl': 'https://example.com/preview.jpg', 'dayTextColor': 'black', 'nightTextColor': 'white'}
        self.assertEqual(await self.store.global_appearance(style), style)
        restarted = Keepsakes(self.temp.name, self.buckets, self.store.delete_bucket, self.store.clean_indexes, self.store.queue_embedding)
        self.assertEqual(await restarted.global_appearance(), style)
        second = (await self.store.create(self.body(turn='chat:a:2')))[0]
        self.assertEqual(first['appearance'], second['appearance'])
        self.assertEqual(await self.store.global_appearance(), style)

    async def test_global_images_independent_and_request_atomic(self):
        import base64
        day = 'data:image/png;base64,' + base64.b64encode(b'day image').decode()
        night = 'data:image/jpeg;base64,' + base64.b64encode(b'night image').decode()
        result = await self.store.global_appearance({'dayBackgroundUrl': day, 'nightBackgroundUrl': night})
        self.assertNotIn('data:', str(result))
        self.assertEqual(await self.store.global_background('day'), ('image/png', b'day image'))
        self.assertEqual(await self.store.global_background('night'), ('image/jpeg', b'night image'))
        await self.store.global_appearance({'dayBackgroundUrl': result['dayBackgroundUrl'], 'nightTextColor': 'black'})
        before = await self.store.global_appearance()
        with self.assertRaises(ValueError):
            await self.store.global_appearance({'dayBackgroundUrl': '', 'nightBackgroundUrl': 'javascript:alert(1)'})
        self.assertEqual(await self.store.global_appearance(), before)
        await self.store.global_appearance({'dayBackgroundUrl': ''})
        self.assertEqual(await self.store.global_background('night'), ('image/jpeg', b'night image'))
        with self.assertRaises(KeyError):
            await self.store.global_background('day')
        with self.assertRaises(KeyError):
            await self.store.global_background('../other')

if __name__ == '__main__':
    unittest.main()
