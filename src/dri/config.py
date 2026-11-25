"""Configuration loading and validation for DRI agent."""

import asyncio
from fnmatch import fnmatchcase
from urllib.parse import urlsplit
from pathlib import Path
from typing import Dict, Any, Optional, List, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
import yaml
import os
import re

from .core.log_parser import DEFAULT_CRASH, DEFAULT_ERRORS

from .utils.logger import get_logger

logger = get_logger("config")


class ThresholdRule(BaseModel):
    """Threshold rule for policy evaluation."""
    metric: str  # "cpu", "memory", "growth_rate"
    operator: str  # ">", "<", ">="
    value: float
    duration_seconds: int = 60  # How long before triggering
    action: str  # "warning", "throttle", "isolate", "restart"

    @field_validator("operator")
    @classmethod
    def validate_operator(cls, v: str) -> str:
        if v not in [">", "<", ">=", "<=", "=="]:
            raise ValueError(f"Invalid operator: {v}")
        return v

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in ["warning", "throttle", "isolate", "restart", "webhook"]:
            raise ValueError(f"Invalid action: {v}")
        return v


class ContainerPolicy(BaseModel):
    """Policy for a container or image."""
    name: str
    selector: Dict[str, str] = Field(default_factory=dict)  # Labels or image patterns
    rules: List[ThresholdRule] = Field(default_factory=list)
    enabled: bool = True


class ThrottleConfig(BaseModel):
    """CPU/Memory throttling configuration."""
    cpu_shares: Optional[int] = None
    cpu_quota: Optional[int] = None  # microseconds
    memory_limit: Optional[str] = None  # e.g., "512m"
    memory_reservation: Optional[str] = None


class ActionConfig(BaseModel):
    """Configuration for actions."""
    throttle: ThrottleConfig = Field(default_factory=ThrottleConfig)
    isolate_cpuset: Optional[str] = None  # e.g., "0-1"
    webhook_url: Optional[str] = None
    restart_grace_period: int = 30  # seconds


class StorageConfig(BaseModel):
    """Storage backend configuration."""
    backend: str = "sqlite"
    
    sqlite_path: str = "/var/lib/dri/metrics.db"
    # Must be >= recommender lookback_days * 24; default matches 7-day lookback.
    retention_hours: int = 168

    # InfluxDB (legacy optional integration)
    influxdb_enabled: bool = False
    influxdb_url: Optional[str] = None
    influxdb_token: Optional[str] = None
    influxdb_org: Optional[str] = None
    influxdb_bucket: Optional[str] = None

    # TimescaleDB (optional alternative backend — Phase 6 spike)
    # Enable by setting backend: "timescaledb" and providing a DSN.
    # Requires: pip install "docker-resource-intelligence[timescaledb]"
    #
    # Example DSN: "postgresql://dri:secret@timescaledb:5432/dri"
    timescaledb_url: Optional[str] = None

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, v: str) -> str:
        if v not in ["sqlite", "timescaledb"]:
            raise ValueError(f"Invalid storage backend: {v}")
        return v


class APIConfig(BaseModel):
    """HTTP API configuration."""
    host: str = "0.0.0.0"
    port: int = 8000
    enable_cors: bool = True
    allowed_origins: List[str] = Field(default_factory=lambda: ["*"])
    prometheus_metrics: bool = True
    api_key: Optional[str] = None


class LLMConfig(BaseModel):
    enabled: bool = False
    provider: Literal["ollama"] = "ollama"
    base_url: str = "http://ollama:11434"
    model: str = Field("qwen2.5:1.5b", min_length=1)
    timeout_seconds: int = Field(60, gt=0)
    max_input_chars: int = Field(1500, gt=0)
    max_output_tokens: int = Field(200, gt=0)
    num_ctx: int = Field(2048, gt=0)
    temperature: float = Field(0.0, ge=0)
    cache_minutes: int = Field(10, gt=0)
    queue_size: int = Field(20, gt=0)
    probe_interval_seconds: int = Field(30, gt=0)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("base_url must be an HTTP(S) URL with a host")
        return value


class LogConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory_growth_pct: float = Field(5, gt=0)
    cpu_saturation_pct: float = Field(100, gt=0)
    surge_warmup_minutes: int = Field(30, ge=0)
    warmup_minutes: int = Field(10, ge=0)
    enabled: bool = True
    retention_hours: int = Field(24, gt=0)
    max_line_length: int = Field(1000, gt=0)
    max_lines_per_second: int = Field(200, gt=0)
    flush_interval_seconds: float = Field(2.0, gt=0)
    flush_batch_size: int = Field(200, gt=0)
    discover_interval_seconds: int = Field(15, gt=0)
    max_followed_containers: int = Field(50, gt=0)
    error_patterns: List[str] = Field(default_factory=lambda: list(DEFAULT_ERRORS))
    crash_patterns: List[str] = Field(default_factory=lambda: list(DEFAULT_CRASH))

    @field_validator("error_patterns", "crash_patterns")
    @classmethod
    def validate_patterns(cls, patterns: List[str]) -> List[str]:
        for pattern in patterns:
            try:
                re.compile(pattern, re.I)
            except re.error as exc:
                raise ValueError(f"Invalid log regex {pattern!r}: {exc}") from exc
        return patterns


class RecommenderConfig(BaseModel):
    """Configurable thresholds for long-term memory trend confidence."""
    leak_slope_threshold_pct_per_hour: float = Field(0.5, ge=0.0)
    trend_min_elapsed_minutes: float = Field(30.0, ge=0.0)
    trend_min_distinct_samples: int = Field(20, ge=2)
    trend_required_growing_windows: int = Field(3, ge=1)
    trend_window_width_minutes: int = Field(10, ge=1)


class DRIConfig(BaseModel):
    """Main DRI configuration."""
    model_config = ConfigDict(hide_input_in_errors=True)
    log_level: str = "INFO"
    log_json: bool = False
    log_file: Optional[str] = None

    collector_interval: int = 5  # seconds
    aggregator_interval: int = 10  # seconds
    policy_check_interval: int = 10  # seconds
    # How often (seconds) the background worker recomputes and caches
    # recommendations and forecasts for all known containers.
    recommendation_cache_interval: int = 3600  # 1 hour
    # How often (seconds) to poll all containers (incl. stopped) to detect
    # status transitions.  15 s is a good balance between responsiveness and
    # Docker API overhead.
    container_status_interval: int = 15  # seconds
    # How many minutes after a container stops before its metrics window is
    # evicted from the in-memory aggregator.  Keeping it at 5 min lets the
    # dashboard show recent history briefly without lingering for 24 h.
    container_status_cleanup_minutes: int = 5

    logs: LogConfig = Field(default_factory=LogConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    actions: ActionConfig = Field(default_factory=ActionConfig)
    recommender: RecommenderConfig = Field(default_factory=RecommenderConfig)

    # Alert re-notify: once an alert fires for a violation episode, suppress
    # repeat alerts for this many seconds (unless the breach clears and restarts).
    # Default 300 s (5 min). Set to 0 to disable de-duplication.
    alert_renotify_seconds: int = 300

    # Global and per-container policies
    policy_exclude_names: List[str] = Field(default_factory=lambda: ["dri-*"])

    def excludes_name(self, name: str) -> bool:
        return any(fnmatchcase(name, pattern) for pattern in self.policy_exclude_names)

    global_policy: ContainerPolicy = Field(default_factory=lambda: ContainerPolicy(name="global"))
    container_policies: List[ContainerPolicy] = Field(default_factory=list)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        if v not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            raise ValueError(f"Invalid log level: {v}")
        return v


def load_config(config_path: Optional[str] = None) -> DRIConfig:
    """
    Load DRI configuration from YAML file.

    Args:
        config_path: Path to config file. If None, uses DRI_CONFIG env var or default.

    Returns:
        Parsed DRIConfig instance
    """
    if not config_path:
        config_path = os.environ.get("DRI_CONFIG", "config/config.yaml")

    config_file = Path(config_path)

    config_data = {}
    if config_file.exists():
        logger.info(f"Loading config from {config_file}")
        with config_file.open() as f:
            config_data = yaml.safe_load(f) or {}
    else:
        logger.warning(f"Config file not found: {config_file}, using defaults")

    try:
        for field in ("enabled", "base_url", "model"):
            env_name = "DRI_LLM_" + field.upper()
            if env_name in os.environ:
                config_data.setdefault("logs", {}).setdefault("llm", {})[field] = os.environ[env_name]
                logger.info(f"Overriding logs.llm.{field} from {env_name}")
        config = DRIConfig(**config_data)
        logger.info(f"Config loaded successfully")
        return config
    except Exception as e:
        logger.error(f"Config validation failed: {e}")
        raise


async def watch_config(
    config_path: str,
    on_reload: Any,
    check_interval: int = 5,
) -> None:
    """
    Watch config file for changes and trigger reload callback.

    Args:
        config_path: Path to config file
        on_reload: Async callable to invoke on config change
        check_interval: Check interval in seconds
    """
    config_file = Path(config_path)
    last_mtime = config_file.stat().st_mtime if config_file.exists() else 0

    while True:
        try:
            await asyncio.sleep(check_interval)

            if not config_file.exists():
                continue

            current_mtime = config_file.stat().st_mtime
            if current_mtime > last_mtime:
                logger.info(f"Config file changed, reloading...")
                try:
                    new_config = load_config(str(config_file))
                    await on_reload(new_config)
                    last_mtime = current_mtime
                except Exception as e:
                    logger.error(f"Failed to reload config: {e}")
        except Exception as e:
            logger.error(f"Config watcher error: {e}")
            await asyncio.sleep(5)
