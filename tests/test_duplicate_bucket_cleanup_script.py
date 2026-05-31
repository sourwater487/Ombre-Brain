import asyncio
import importlib.util
import sys
from pathlib import Path


def _load_cleanup_module():
    path = Path("scripts/cleanup_duplicate_buckets.py")
    spec = importlib.util.spec_from_file_location("cleanup_duplicate_buckets", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bucket(bucket_id, content, **meta):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": bucket_id,
            "type": "dynamic",
            "importance": 5,
            **meta,
        },
    }


class FakeBucketManager:
    def __init__(self):
        self.deleted = []

    async def delete(self, bucket_id):
        self.deleted.append(bucket_id)
        return True


class FakeEmbeddingEngine:
    def __init__(self):
        self.deleted = []

    def delete_embedding(self, bucket_id):
        self.deleted.append(bucket_id)


def test_exact_duplicate_plan_keeps_important_bucket_and_deletes_dynamic_copy():
    cleanup = _load_cleanup_module()
    buckets = [
        _bucket("keep", "这条测试记忆用于验证重复清理。", importance=9),
        _bucket("copy", "这条测试记忆用于验证重复清理。", importance=5),
    ]

    plans = cleanup.exact_duplicate_plans(buckets, min_chars=5)

    assert len(plans) == 1
    assert plans[0].keep_id == "keep"
    assert plans[0].delete_ids == ["copy"]


def test_exact_duplicate_plan_never_deletes_pinned_permanent_protected_or_anchor():
    cleanup = _load_cleanup_module()
    duplicate_text = "某个项目记录被重复写入，需要保留安全边界。"
    buckets = [
        _bucket("safe-copy", duplicate_text, importance=4),
        _bucket("pinned", duplicate_text, pinned=True, importance=7),
        _bucket("permanent", duplicate_text, type="permanent", importance=8),
        _bucket("protected", duplicate_text, protected=True, importance=9),
        _bucket("anchor", duplicate_text, anchor=True, importance=6),
    ]

    plans = cleanup.exact_duplicate_plans(buckets, min_chars=5)
    delete_ids = set(plans[0].delete_ids)

    assert delete_ids == {"safe-copy"}
    assert not {"pinned", "permanent", "protected", "anchor"} & delete_ids


def test_exact_duplicate_plan_does_not_delete_bucket_with_comments():
    cleanup = _load_cleanup_module()
    duplicate_text = "这条测试记忆有一个带评论的重复桶。"
    buckets = [
        _bucket("keep", duplicate_text, importance=9),
        _bucket("commented", duplicate_text, comments=[{"content": "保留这条评论"}], importance=4),
        _bucket("plain-copy", duplicate_text, importance=4),
    ]

    plans = cleanup.exact_duplicate_plans(buckets, min_chars=5)

    assert plans[0].delete_ids == ["plain-copy"]
    assert "commented" not in plans[0].delete_ids


def test_exact_duplicate_plan_ignores_affect_anchor_difference():
    cleanup = _load_cleanup_module()
    buckets = [
        _bucket("keep", "一份设备采购计划需要在周五前确认。", importance=8),
        _bucket(
            "copy",
            (
                "一份设备采购计划需要在周五前确认。\n\n"
                "### affect_anchor\n\n"
                "> 会议室里放着采购清单。\n"
                "> Cmaj7 -> G6\n\n"
                "含义：这是额外标记。"
            ),
            importance=5,
        ),
    ]

    plans = cleanup.exact_duplicate_plans(buckets, min_chars=5)

    assert len(plans) == 1
    assert plans[0].keep_id == "keep"
    assert plans[0].delete_ids == ["copy"]


def test_near_duplicate_pairs_are_reported_for_review_only():
    cleanup = _load_cleanup_module()
    buckets = [
        _bucket("a", "Weekend itinerary includes a train booking, a museum visit, and a hotel check-in."),
        _bucket("b", "The weekend travel plan includes booking the train, visiting the museum, and checking in at the hotel."),
        _bucket("c", "A hardware purchase plan lists monitors, keyboards, and delivery dates."),
    ]

    pairs = cleanup.near_duplicate_pairs(buckets, threshold=70, min_chars=10)

    assert pairs[0][0:2] == ("a", "b")
    assert pairs[0][2] >= 70


def test_exact_duplicate_pairs_do_not_enter_near_duplicate_pairs_when_excluded():
    cleanup = _load_cleanup_module()
    buckets = [
        _bucket("a", "完全重复的测试记录应该只出现在 exact 结果。"),
        _bucket("b", "完全重复的测试记录应该只出现在 exact 结果。"),
    ]

    exact_plans = cleanup.exact_duplicate_plans(buckets, min_chars=5)
    exact_pairs = {
        frozenset((left_id, right_id))
        for plan in exact_plans
        for index, left_id in enumerate(plan.bucket_ids)
        for right_id in plan.bucket_ids[index + 1:]
    }
    pairs = cleanup.near_duplicate_pairs(buckets, threshold=80, min_chars=5, exclude_pairs=exact_pairs)

    assert pairs == []


def test_near_duplicate_pairs_include_protected_bucket_with_safe_copy():
    cleanup = _load_cleanup_module()
    buckets = [
        _bucket("protected", "Project schedule includes a design review, a test pass, and a Friday release.", protected=True),
        _bucket("copy", "The project schedule has a design review, test pass, and Friday release."),
    ]

    pairs = cleanup.near_duplicate_pairs(buckets, threshold=75, min_chars=10)

    assert pairs[0][0:2] == ("protected", "copy")


def test_suggested_near_action_keeps_protected_and_deletes_safe_copy():
    cleanup = _load_cleanup_module()
    buckets = {
        "protected": _bucket(
            "protected",
            "Project schedule includes a design review, a test pass, and a Friday release.",
            protected=True,
        ),
        "copy": _bucket("copy", "The project schedule has a design review, test pass, and Friday release."),
    }

    assert cleanup.suggested_near_action("protected", "copy", buckets) == ("protected", "copy")


def test_interactive_exact_group_y_deletes_copy_and_embedding(monkeypatch):
    cleanup = _load_cleanup_module()
    bucket_mgr = FakeBucketManager()
    embedding_engine = FakeEmbeddingEngine()
    buckets = {
        "keep": _bucket("keep", "这条测试记忆用于交互式精确清理。", importance=9),
        "copy": _bucket("copy", "这条测试记忆用于交互式精确清理。", importance=5),
    }
    plan = cleanup.DuplicatePlan(
        key="same",
        keep_id="keep",
        delete_ids=["copy"],
        bucket_ids=["keep", "copy"],
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    deleted = asyncio.run(cleanup.interactive_cleanup(bucket_mgr, embedding_engine, [plan], [], buckets))

    assert deleted == ["copy"]
    assert bucket_mgr.deleted == ["copy"]
    assert embedding_engine.deleted == ["copy"]


def test_interactive_near_duplicate_y_deletes_suggested_safe_side(monkeypatch):
    cleanup = _load_cleanup_module()
    bucket_mgr = FakeBucketManager()
    embedding_engine = FakeEmbeddingEngine()
    buckets = {
        "protected": _bucket(
            "protected",
            "Project schedule includes a design review, a test pass, and a Friday release.",
            protected=True,
        ),
        "copy": _bucket("copy", "The project schedule has a design review, test pass, and Friday release."),
    }
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    deleted = asyncio.run(
        cleanup.interactive_cleanup(bucket_mgr, embedding_engine, [], [("protected", "copy", 82.0)], buckets)
    )

    assert deleted == ["copy"]
    assert bucket_mgr.deleted == ["copy"]
    assert embedding_engine.deleted == ["copy"]


def test_interactive_near_duplicate_side_number_deletes_only_if_safe(monkeypatch):
    cleanup = _load_cleanup_module()
    bucket_mgr = FakeBucketManager()
    embedding_engine = FakeEmbeddingEngine()
    buckets = {
        "protected": _bucket(
            "protected",
            "Project schedule includes a design review, a test pass, and a Friday release.",
            protected=True,
        ),
        "copy": _bucket("copy", "The project schedule has a design review, test pass, and Friday release."),
    }
    answers = iter(["1", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    deleted = asyncio.run(
        cleanup.interactive_cleanup(
            bucket_mgr,
            embedding_engine,
            [],
            [("protected", "copy", 82.0), ("protected", "copy", 82.0)],
            buckets,
        )
    )

    assert deleted == ["copy"]
    assert bucket_mgr.deleted == ["copy"]
    assert embedding_engine.deleted == ["copy"]
