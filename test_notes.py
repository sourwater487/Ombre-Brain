import sys
sys.path.insert(0, '/app')
import tempfile, unittest
from pathlib import Path
from keepsakes import Keepsakes
from reminder_store import ReminderStore
from test_keepsakes import Buckets, CARD

class Notes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.memos=ReminderStore({'state_dir':self.temp.name})
        self.buckets=Buckets();self.embedded=[]
        self.store=Keepsakes(self.temp.name,self.buckets,None,None,self.embedded.append,reminders=self.memos)
        self.body={'turnId':'chat:a:1','conversationId':'a','cards':[{'type':'note','content':'周末一起整理照片。','start_at':'2099-09-20','end_at':'2099-09-22'}]}
    async def asyncTearDown(self):self.temp.cleanup()
    async def test_same_record_status_content_dates_and_retry(self):
        first=(await self.store.create(self.body))[0]
        self.assertEqual(first['status'],'active');self.assertEqual(first['startAt'],'2099-09-20')
        self.assertFalse(self.buckets.rows);self.assertFalse(self.embedded)
        self.assertEqual(self.memos.get(first['reminderId'])['content'],first['content'])
        self.memos.update(first['reminderId'],status='done',content='已经整理好了。',end_at='2099-09-23')
        again=(await self.store.create(self.body))[0]
        self.assertEqual(again['status'],'done');self.assertEqual(again['content'],'已经整理好了。');self.assertEqual(again['endAt'],'2099-09-23')
        self.assertEqual(len(self.memos.list(status='all')),1)
        await self.store.bind('a','chat:a:1','2');self.assertEqual((await self.store.list())[0]['messageId'],'2')
        await self.store.delete(first['id'],'DELETE')
        self.assertEqual(self.memos.get(first['id'])['status'],'archived');self.assertEqual(await self.store.list(),[])
        await self.store.create(self.body);self.assertEqual(len(self.memos.list(status='all')),1);self.assertEqual(await self.store.list(),[])
    async def test_dates_optional_expiry_and_validation_before_write(self):
        self.body['cards'][0]={'type':'note','content':'下次一起整理照片。'}
        note=(await self.store.create(self.body))[0];self.assertEqual(note['startAt'],'');self.assertEqual(note['endAt'],'')
        self.memos.update(note['id'],end_at='2000-01-01')
        self.assertEqual((await self.store.list())[0]['status'],'archived')
        self.body['turnId']='chat:a:2';self.body['cards']=[CARD,{'type':'note','content':'bad','start_at':'2099-09-30','end_at':'2099-09-20'}]
        with self.assertRaises(ValueError):await self.store.create(self.body)
        self.assertFalse(self.buckets.rows)
    async def test_all_create_parameters_match_reminder_contract(self):
        import ast, keepsakes
        module=ast.parse(Path(keepsakes.__file__).with_name('server.py').read_text())
        tool=next(n for n in module.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='reminder_create')
        self.assertFalse(tool.decorator_list)
        args=dict(title='操作台标题',content='我会记得这件事。',start_at='2099-01-01',end_at='2099-12-31',
            next_due_at='2099-01-02T20:00:00+08:00',repeat_rule='daily',interval_rounds=9,
            cooldown_minutes=30,daily_limit=2,max_injections=8,channel='chat',session_id='session-exact')
        self.assertEqual(set(args),{arg.arg for arg in tool.args.args})
        self.body['cards']=[dict(type='note',**args)]
        note=(await self.store.create(self.body))[0]
        record=self.memos.get(note['reminderId'])
        for key,value in args.items():self.assertEqual(record[key],value,key)
        self.assertEqual(note['title'],'')
        self.assertNotIn('repeat_rule',note)
        self.assertEqual(record['source'],'keepsake_note')
        self.assertFalse(self.buckets.rows);self.assertFalse(self.embedded)
        self.memos.update(note['id'],daily_limit=3,repeat_rule='once')
        await self.store.create(self.body)
        self.assertEqual(self.memos.get(note['id'])['daily_limit'],3)
        self.assertEqual(self.memos.get(note['id'])['repeat_rule'],'once')
    async def test_repeat_defaults_and_explicit_unlimited(self):
        for index,(rule,limit,expected) in enumerate([('every_n_rounds',-1,1),('morning_evening',-1,2),('daily',0,0),('once',1,1)]):
            self.body['turnId']=f'chat:a:{index+10}'
            self.body['cards']=[dict(type='note',content='我会记住。',repeat_rule=rule,daily_limit=limit)]
            note=(await self.store.create(self.body))[0]
            self.assertEqual(self.memos.get(note['id'])['daily_limit'],expected)
    async def test_invalid_parameters_reject_entire_batch(self):
        invalid=[dict(repeat_rule='weekly'),dict(interval_rounds=True),dict(cooldown_minutes=-1),
            dict(daily_limit=1.5),dict(max_injections='3'),dict(next_due_at='not-a-date'),
            dict(channel=[]),dict(session_id=None),dict(unrecognized='ignored?')]
        for fields in invalid:
            self.body['cards']=[CARD,dict(type='note',content='我会记住。',**fields)]
            with self.assertRaises(ValueError):await self.store.create(self.body)
            self.assertFalse(self.buckets.rows)
            self.assertEqual(self.memos.list(status='all'),[])
            self.assertEqual(self.store.read(),{})
    async def test_global_backgrounds_independent(self):
        raw='data:image/png;base64,YWJj'
        note=await self.store.global_appearance({'dayBackgroundUrl':raw,'nightBackgroundUrl':'https://example.com/night','previewBackgroundUrl':'https://example.com/preview'},'note')
        self.assertTrue(note['dayBackgroundUrl'].startswith('/api/cards/note-appearance/'))
        self.assertEqual(await self.store.global_appearance(),{})
        self.assertEqual(await self.store.global_background('day','note'),('image/png',b'abc'))
        await self.store.global_appearance({'dayBackgroundUrl':'https://example.com/ramble'})
        self.assertEqual((await self.store.global_appearance(kind='note'))['dayBackgroundUrl'],note['dayBackgroundUrl'])

if __name__=='__main__':unittest.main()
