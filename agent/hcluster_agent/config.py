"""Configuration loading + validation.

The YAML file holds NON-secret settings. The API token is read from the environment
(HCLUSTER_TOKEN, normally injected by systemd EnvironmentFile) or from `api.token_file`.
Tokens are never stored in the YAML file.
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import yaml

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
METRIC_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(Exception):
    pass


@dataclass
class ServiceCheck:
    key: str
    name: str
    type: str                      # http | tcp
    enabled: bool = True
    url: str | None = None         # http
    host: str | None = None        # tcp
    port: int | None = None        # tcp
    expected_status: list[tuple[int, int]] = field(default_factory=lambda: [(200, 399)])
    expect_body_contains: str | None = None
    timeout_seconds: float = 5.0
    interval_seconds: int = 60
    degraded_after_ms: int | None = 3000
    verify_tls: bool = True
    retries: int = 1
    report_target: bool = True

    def target(self) -> str:
        return self.url if self.type == "http" else f"{self.host}:{self.port}"


@dataclass
class PromQuery:
    name: str
    expr: str
    reduce: str = "first"          # first | sum | avg | max | min
    enabled: bool = True


@dataclass
class PrometheusConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:9091"
    selector: str = ""
    timeout_seconds: float = 5.0
    collect_targets: bool = True
    queries: list[PromQuery] = field(default_factory=list)


@dataclass
class Config:
    api_url: str
    node_id: str
    token: str
    heartbeat_seconds: int
    metrics_seconds: int
    request_timeout_seconds: float
    max_queue: int
    state_dir: str
    local_fallback: bool
    net_exclude_prefixes: list[str]
    disk_mountpoint: str
    prometheus: PrometheusConfig
    services: list[ServiceCheck]
    log_level: str = "INFO"


def parse_status_ranges(value: Any) -> list[tuple[int, int]]:
    """Accepts 200, "200-399", or a list of those."""
    if value is None:
        return [(200, 399)]
    items = value if isinstance(value, list) else [value]
    out: list[tuple[int, int]] = []
    for it in items:
        if isinstance(it, int):
            out.append((it, it))
        elif isinstance(it, str) and re.fullmatch(r"\d{3}(-\d{3})?", it.strip()):
            lo, _, hi = it.strip().partition("-")
            out.append((int(lo), int(hi or lo)))
        else:
            raise ConfigError(f"invalid expected_status entry: {it!r}")
    return out


def _req(d: dict, key: str, ctx: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"{ctx}: missing required field '{key}'")
    return d[key]


def validate_api_url(url: str) -> str:
    p = urlparse(url)
    if p.scheme == "https" and p.hostname:
        return url.rstrip("/")
    if p.scheme == "http" and p.hostname in LOCAL_HOSTS:   # local development / tests only
        return url.rstrip("/")
    raise ConfigError("api.url must be https:// (plain http is only allowed for localhost development)")


def load_token(api: dict, env: dict[str, str]) -> str:
    tok = env.get("HCLUSTER_TOKEN", "").strip()
    if not tok and api.get("token_file"):
        try:
            with open(api["token_file"], "r", encoding="utf-8") as fh:
                tok = fh.read().strip()
        except OSError as e:
            raise ConfigError(f"cannot read api.token_file: {e}") from e
    if not tok:
        raise ConfigError("no API token: set HCLUSTER_TOKEN (e.g. via systemd EnvironmentFile) or api.token_file")
    return tok


def parse_services(raw: list[dict] | None) -> list[ServiceCheck]:
    out: list[ServiceCheck] = []
    seen: set[str] = set()
    for i, s in enumerate(raw or []):
        ctx = f"services[{i}]"
        key = str(_req(s, "key", ctx))
        if not KEY_RE.match(key):
            raise ConfigError(f"{ctx}: key must match {KEY_RE.pattern}")
        if key in seen:
            raise ConfigError(f"{ctx}: duplicate key '{key}'")
        seen.add(key)
        typ = str(_req(s, "type", ctx)).lower()
        if typ not in ("http", "tcp"):
            raise ConfigError(f"{ctx}: type must be http or tcp")
        sc = ServiceCheck(
            key=key, name=str(s.get("name") or key), type=typ,
            enabled=bool(s.get("enabled", True)),
            timeout_seconds=float(s.get("timeout_seconds", 5)),
            interval_seconds=int(s.get("interval_seconds", 60)),
            degraded_after_ms=(None if s.get("degraded_after_ms", 3000) is None else int(s.get("degraded_after_ms", 3000))),
            verify_tls=bool(s.get("verify_tls", True)),
            retries=int(s.get("retries", 1)),
            report_target=bool(s.get("report_target", True)),
            expect_body_contains=s.get("expect_body_contains"),
        )
        if typ == "http":
            url = str(_req(s, "url", ctx))
            if urlparse(url).scheme not in ("http", "https"):
                raise ConfigError(f"{ctx}: url must be http(s)")
            sc.url = url
            sc.expected_status = parse_status_ranges(s.get("expected_status"))
        else:
            sc.host = str(s.get("host", "127.0.0.1"))
            sc.port = int(_req(s, "port", ctx))
            if not 1 <= sc.port <= 65535:
                raise ConfigError(f"{ctx}: port out of range")
        if sc.interval_seconds < 5 or sc.timeout_seconds <= 0 or sc.retries < 0:
            raise ConfigError(f"{ctx}: interval_seconds>=5, timeout_seconds>0, retries>=0 required")
        out.append(sc)
    return out


def parse_prometheus(raw: dict | None) -> PrometheusConfig:
    raw = raw or {}
    pc = PrometheusConfig(
        enabled=bool(raw.get("enabled", False)),
        base_url=str(raw.get("base_url", "http://127.0.0.1:9091")).rstrip("/"),
        selector=str(raw.get("selector", "") or "").strip(),
        timeout_seconds=float(raw.get("timeout_seconds", 5)),
        collect_targets=bool(raw.get("collect_targets", True)),
    )
    if urlparse(pc.base_url).scheme not in ("http", "https"):
        raise ConfigError("prometheus.base_url must be http(s)")
    seen: set[str] = set()
    for i, q in enumerate(raw.get("queries") or []):
        ctx = f"prometheus.queries[{i}]"
        name = str(_req(q, "name", ctx))
        if not METRIC_RE.match(name):
            raise ConfigError(f"{ctx}: name must match {METRIC_RE.pattern}")
        if name in seen:
            raise ConfigError(f"{ctx}: duplicate name '{name}'")
        seen.add(name)
        red = str(q.get("reduce", "first")).lower()
        if red not in ("first", "sum", "avg", "max", "min"):
            raise ConfigError(f"{ctx}: reduce must be first|sum|avg|max|min")
        pc.queries.append(PromQuery(name=name, expr=str(_req(q, "expr", ctx)), reduce=red, enabled=bool(q.get("enabled", True))))
    return pc


def load_config(path: str, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    api = raw.get("api") or {}
    node = raw.get("node") or {}
    node_id = str(_req(node, "id", "node"))
    try:
        uuid.UUID(node_id)
    except ValueError as e:
        raise ConfigError("node.id must be the UUID shown in the dashboard when you added the node") from e

    iv = raw.get("intervals") or {}
    hb = int(iv.get("heartbeat_seconds", 60))
    mt = int(iv.get("metrics_seconds", 60))
    if hb < 5 or mt < 5:
        raise ConfigError("intervals must be >= 5 seconds")

    loc = raw.get("local_metrics") or {}
    cfg = Config(
        api_url=validate_api_url(str(_req(api, "url", "api"))),
        node_id=node_id,
        token=load_token(api, env),
        heartbeat_seconds=hb,
        metrics_seconds=mt,
        request_timeout_seconds=float(api.get("timeout_seconds", 10)),
        max_queue=int(api.get("max_queue", 720)),
        state_dir=str(raw.get("state_dir", "/var/lib/hcluster-agent")),
        local_fallback=bool(loc.get("fallback", True)),
        net_exclude_prefixes=list(loc.get("net_exclude_prefixes", ["lo", "docker", "veth", "br-", "virbr", "tailscale"])),
        disk_mountpoint=str(loc.get("disk_mountpoint", "/")),
        prometheus=parse_prometheus(raw.get("prometheus")),
        services=parse_services(raw.get("services")),
        log_level=str(raw.get("log_level", "INFO")).upper(),
    )
    return cfg
