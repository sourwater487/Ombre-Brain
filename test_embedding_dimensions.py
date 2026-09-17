import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from embedding_engine import EmbeddingEngine


class EmbeddingDimensionsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = EmbeddingEngine({
            'buckets_dir': self.directory.name,
            'embedding': {'model': 'Qwen/Qwen3-Embedding-8B', 'enabled': False},
        })
        self.create = AsyncMock(return_value=SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1] * 1024)]))
        self.engine.client = SimpleNamespace(embeddings=SimpleNamespace(create=self.create))

    def test_document_and_query_request_1024_dimensions(self):
        for kind in ('document', 'query'):
            vector = asyncio.run(self.engine._generate_embedding('test', kind=kind))
            self.assertEqual(len(vector), 1024)
            self.assertEqual(self.create.call_args.kwargs['dimensions'], 1024)
            self.assertEqual(self.create.call_args.kwargs['timeout'], 180.0)

    def test_wrong_api_dimension_is_not_stored(self):
        self.engine.enabled = True
        self.create.return_value.data[0].embedding = [0.1] * 4096
        self.assertFalse(asyncio.run(self.engine.generate_and_store('test', 'test')))
        self.assertIsNone(asyncio.run(self.engine.get_embedding('test')))

    def test_wrong_stored_dimension_and_model_are_rejected(self):
        self.engine._store_embedding('bad', [0.1] * 4096)
        self.assertIsNone(asyncio.run(self.engine.get_embedding('bad')))
        self.assertEqual(asyncio.run(self.engine.get_embeddings(['bad'])), {})
        self.assertFalse(self.engine._row_matches_current_model('Qwen3-Embedding-8B', 1024, [0.1] * 1024))
        self.engine._store_embedding('good', [0.1] * 1024)
        self.assertEqual(len(asyncio.run(self.engine.get_embedding('good'))), 1024)

    def test_hot_model_changes_update_dimension_policy(self):
        self.engine.model = 'Qwen3-Embedding-8B'
        self.assertEqual(self.engine.dimensions, 1024)
        self.engine.model = 'other-model'
        asyncio.run(self.engine._generate_embedding('test'))
        self.assertNotIn('dimensions', self.create.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
