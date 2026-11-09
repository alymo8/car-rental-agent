"""Runtime settings: thresholds, HTTP policy, models and paths, overridable by env vars.

Each field's env var is its name in upper case (e.g. MAX_AUTO_CHARGE_USD).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

_PATH_FIELDS = ("log_dir", "cache_dir", "kb_path")


@dataclass(frozen=True)
class Settings:
    max_auto_charge_usd: float = 500.0
    max_extension_days: int = 14
    grace_period_minutes: int = 30
    short_extension_minutes: int = 30
    quote_ttl_minutes: int = 15
    max_email_failures: int = 2
    max_payment_failures: int = 2
    http_connect_timeout_s: float = 3.0
    http_read_timeout_s: float = 10.0
    http_availability_read_timeout_s: float = 12.0
    http_max_attempts: int = 3
    http_backoff_base_s: float = 0.5
    http_backoff_cap_s: float = 4.0
    openai_model: str = "gpt-5.4-mini"
    embedding_model: str = "text-embedding-3-small"
    log_dir: str = "logs"
    cache_dir: str = ".cache"
    kb_path: str = "data/knowledge-base/articles.json"
    roadway_api_url: str = ""
    roadway_api_key: str = ""


_CASTS = {"float": float, "int": int, "str": str}


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from `env` (or os.environ after load_dotenv() when None)."""
    if env is None:
        load_dotenv()
        env = os.environ
    values: dict[str, object] = {}
    for f in fields(Settings):
        raw = env.get(f.name.upper())
        if raw is None or raw.strip() == "":
            value = f.default
        else:
            value = _CASTS[f.type](raw.strip())
        if f.name in _PATH_FIELDS:
            path = Path(str(value))
            if not path.is_absolute():
                path = REPO_ROOT / path
            value = str(path)
        values[f.name] = value
    return Settings(**values)
