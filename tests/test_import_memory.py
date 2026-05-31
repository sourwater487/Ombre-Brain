import asyncio

from import_memory import ImportEngine, _import_similarity_text


class DummyBucketManager:
    def __init__(self):
        self.search_query = None
        self.updated = None

    async def search(self, query: str, **kwargs) -> list[dict]:
        self.search_query = query
        return [
            {
                "id": "existing",
                "content": "正文相同",
                "score": 99,
                "metadata": {
                    "tags": [],
                    "importance": 5,
                    "domain": ["测试"],
                    "valence": 0.5,
                    "arousal": 0.3,
                },
            }
        ]

    async def update(self, bucket_id: str, **kwargs):
        self.updated = {"id": bucket_id, **kwargs}


class DummyDehydrator:
    async def merge(self, old_content: str, new_content: str) -> str:
        return old_content


def test_import_similarity_text_ignores_affect_anchor_differences():
    base = "正文相同"
    anchored = "正文相同\n\n### affect_anchor\n\n> only-import-anchor"

    assert _import_similarity_text(base) == _import_similarity_text(anchored)


def test_import_merge_search_strips_affect_anchor(tmp_path):
    bucket_mgr = DummyBucketManager()
    engine = ImportEngine(
        {"buckets_dir": str(tmp_path), "merge_threshold": 75},
        bucket_mgr,
        DummyDehydrator(),
    )

    merged = asyncio.run(
        engine._merge_or_create_item(
            {
                "content": "正文相同\n\n### affect_anchor\n\n> only-import-anchor",
                "domain": ["测试"],
                "tags": [],
                "importance": 5,
            }
        )
    )

    assert merged is True
    assert bucket_mgr.search_query == "正文相同"
    assert "only-import-anchor" not in bucket_mgr.search_query
