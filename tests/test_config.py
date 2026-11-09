from pathlib import Path

from config import load_settings


def test_defaults():
    s = load_settings({})
    assert s.max_auto_charge_usd == 500.0 and s.max_extension_days == 14
    assert s.quote_ttl_minutes == 15 and s.openai_model == "gpt-5.4-mini"
    assert s.http_max_attempts == 3 and s.http_backoff_base_s == 0.5


def test_env_override_and_types():
    s = load_settings({"MAX_AUTO_CHARGE_USD": "750", "MAX_EXTENSION_DAYS": "7"})
    assert s.max_auto_charge_usd == 750.0 and s.max_extension_days == 7


def test_paths_resolve_to_repo_root():
    s = load_settings({})
    assert Path(s.kb_path).is_file()
