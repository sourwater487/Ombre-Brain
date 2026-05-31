from utils import bucket_text_for_embedding, strip_affect_anchor


def test_strip_affect_anchor_removes_section():
    text = "正文\n\n### affect_anchor\n\n> scene\n> Cmaj7\n\n含义：only anchor"

    assert strip_affect_anchor(text) == "正文"


def test_strip_affect_anchor_preserves_following_section():
    text = "正文\n\n### affect_anchor\n\n> scene\n\n### other_section\n\n保留内容"

    stripped = strip_affect_anchor(text)

    assert "affect_anchor" not in stripped
    assert "### other_section\n\n保留内容" in stripped


def test_bucket_text_for_embedding_includes_title_and_content_only():
    text = bucket_text_for_embedding(
        {
            "content": "正文里有 [[双链]]。\n\n### affect_anchor\n\n> silent-token",
            "metadata": {
                "name": "标题 [[记忆]]",
                "comments": [{"content": "一圈 [[年轮]]"}],
            },
        }
    )

    assert "Title: 标题 记忆" in text
    assert "Content: 正文里有 双链。" in text
    assert "affect_anchor" not in text
    assert "silent-token" not in text
    assert "一圈 年轮" not in text


def test_bucket_text_for_embedding_keeps_content_only_shape_without_title():
    assert bucket_text_for_embedding({"content": "只有正文", "metadata": {}}) == "只有正文"
