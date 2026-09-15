import ast
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace

class HoldParameters(unittest.IsolatedAsyncioTestCase):
    async def test_old_arguments_reject_before_any_work(self):
        tree=ast.parse(Path(__file__).with_name("server.py").read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="hold")
        fn.decorator_list=[]
        calls=[]
        async def started():calls.append("started")
        ns={"decay_engine":SimpleNamespace(ensure_started=started),"strip_raw_client_context":str}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),"hold-test","exec"),ns)
        hold=ns["hold"]
        self.assertEqual(set(inspect.signature(hold).parameters),{"content","tags","importance","pinned","valence","arousal","title","date","domain"})
        for args in [{"whisper":True},{"feel":False},{"source_bucket":""}]:
            with self.assertRaises(TypeError):await hold(content="test",**args)
        self.assertEqual(calls,[])
        self.assertEqual(await hold(content=""),"内容为空，无法存储。")
        self.assertEqual(calls,["started"])
