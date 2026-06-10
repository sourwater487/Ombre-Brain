from copy import deepcopy

import pytest

from embedding_engine import EmbeddingEngine


def _cfg(test_config: dict, **embedding_overrides) -> dict:
    cfg = deepcopy(test_config)
    cfg["dehydration"]["api_key"] = "dehy-key"
    cfg["dehydration"]["base_url"] = "https://dehy.example/v1"
    cfg["embedding"] = {
        **cfg["embedding"],
        **embedding_overrides,
    }
    return cfg


def test_embedding_uses_independent_api_config(test_config):
    engine = EmbeddingEngine(
        _cfg(
            test_config,
            api_key="embed-key",
            base_url="https://embed.example/v1",
            model="Qwen/Qwen3-Embedding-0.6B",
        )
    )

    assert engine.api_key == "embed-key"
    assert engine.base_url == "https://embed.example/v1"
    assert engine.model == "Qwen/Qwen3-Embedding-0.6B"
    assert engine.enabled is True


def test_embedding_falls_back_to_dehydration_api_config(test_config):
    engine = EmbeddingEngine(_cfg(test_config, api_key="", base_url=""))

    assert engine.api_key == "dehy-key"
    assert engine.base_url == "https://dehy.example/v1"
    assert engine.enabled is True


def test_embedding_query_uses_instruction(test_config):
    engine = EmbeddingEngine(
        _cfg(
            test_config,
            api_key="embed-key",
            query_instruction="Find relevant memory.",
        )
    )

    assert engine._prepare_embedding_input("猫咪药量", kind="query") == (
        "Instruct: Find relevant memory.\nQuery: 猫咪药量"
    )
    assert engine._prepare_embedding_input("猫咪药量", kind="document") == "猫咪药量"


@pytest.mark.asyncio
async def test_embedding_get_embedding_ignores_old_model_rows(test_config):
    engine = EmbeddingEngine(
        _cfg(
            test_config,
            api_key="embed-key",
            model="Qwen/Qwen3-Embedding-4B",
        )
    )
    engine._store_embedding("bucket-a", [0.1, 0.2, 0.3])

    assert await engine.get_embedding("bucket-a") == [0.1, 0.2, 0.3]

    old_engine = EmbeddingEngine(
        _cfg(
            test_config,
            api_key="embed-key",
            model="Qwen/Qwen3-Embedding-0.6B",
        )
    )
    assert await old_engine.get_embedding("bucket-a") is None
