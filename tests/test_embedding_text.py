from utils import bucket_text_for_embedding


def test_bucket_text_for_embedding_includes_title_content_and_comments():
    text = bucket_text_for_embedding(
        {
            "content": "正文里有 [[双链]]。",
            "metadata": {
                "name": "标题 [[记忆]]",
                "comments": [{"content": "一圈 [[年轮]]"}],
            },
        }
    )

    assert "Title: 标题 记忆" in text
    assert "Content: 正文里有 双链。" in text
    assert "Comments:\n一圈 年轮" in text


def test_bucket_text_for_embedding_keeps_content_only_shape_without_title():
    assert bucket_text_for_embedding({"content": "只有正文", "metadata": {}}) == "只有正文"
