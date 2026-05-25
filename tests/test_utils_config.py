from utils import load_config


def test_load_config_defaults_relationship_weather_off(tmp_path):
    config = load_config(str(tmp_path / "missing-config.yaml"))

    assert config["gateway"]["relationship_weather_interval_rounds"] == 0
    assert config["gateway"]["cooldown_hours"] == 6
    assert config["gateway"]["skip_recent_rounds"] == 5
    assert config["write_path"]["semantic_search_timeout_seconds"] == 3
    assert config["reflection"]["enrich_backfill_enabled"] is True
    assert config["reflection"]["enrich_backfill_limit"] == 5
