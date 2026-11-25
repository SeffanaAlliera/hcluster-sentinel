"""Prometheus HTTP API client (instant queries + target summary).

Rules implemented here (from the project spec):
  * Prometheus is OPTIONAL: every failure is converted into a status, never an exception.
  * A query that returns no data is reported as UNAVAILABLE. Nothing is estimated or invented.
  * Only scalar summaries leave the node. The full series never does.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .config import PrometheusConfig, PromQuery

MAX_RESPONSE = 4 * 1024 * 1024


def expand_selector(expr: str, selector: str) -> str:
    """Replaces $SEL with the configured label matchers and tidies stray commas."""
    out = expr.replace("$SEL", selector)
    out = re.sub(r"\{\s*,", "{", out)
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r",\s*\}", "}", out)
    return out


@dataclass
class PromResult:
    prom_status: str = "disabled"          # ok | unreachable | disabled
    prom_error: str | None = None
    targets_up: int | None = None
    targets_total: int | None = None
    down_targets: list[str] = field(default_factory=list)
    values: dict[tuple[str, str], float] = field(default_factory=dict)   # (name, label) -> value
    unavailable: list[str] = field(default_factory=list)   # no data / error for these
    errors: dict[str, str] = field(default_factory=dict)
    metrics_status: str = "disabled"       # ok | partial | failed | disabled


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401
        return None


def _get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "hcluster-agent/1"})
    with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
        raw = resp.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("response too large")
    return json.loads(raw)


def _reduce(vals: list[float], how: str) -> float:
    if how == "sum":
        return sum(vals)
    if how == "avg":
        return sum(vals) / len(vals)
    if how == "max":
        return max(vals)
    if how == "min":
        return min(vals)
    return vals[0]


MAX_SERIES = 16


def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def instant_query(cfg: PrometheusConfig, q: PromQuery) -> list[tuple[str, float]]:
    """Returns [(label, value)]; [] if the query produced no data. Raises on API/transport error.

    Without `label_by` the result is reduced to one node-wide value (label ''). With `label_by: <prom label>`
    every series is kept, labelled by that label's value (e.g. mountpoint, device), capped at 16 series."""
    expr = expand_selector(q.expr, cfg.selector)
    url = f"{cfg.base_url}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    body = _get_json(url, cfg.timeout_seconds)
    if body.get("status") != "success":
        raise RuntimeError(f"{body.get('errorType', 'error')}: {body.get('error', 'query failed')}"[:200])
    data = body.get("data") or {}
    rtype, result = data.get("resultType"), data.get("result")
    series: list[tuple[str, float]] = []
    if rtype == "vector":
        for item in result or []:
            try:
                v = _finite(item["value"][1])
            except (KeyError, IndexError, TypeError):
                continue
            if v is None:
                continue
            lab = ""
            if q.label_by:
                lab = str((item.get("metric") or {}).get(q.label_by, ""))
                if not lab:
                    continue
            series.append((lab[:64], v))
    elif rtype == "scalar":
        try:
            v = _finite(result[1])
        except (TypeError, IndexError):
            v = None
        if v is not None:
            series.append(("", v))
    else:
        raise RuntimeError(f"unsupported resultType {rtype!r}")
    if q.label_by:
        series = sorted(series)[:MAX_SERIES]
        if q.sum_total and series:
            series.append(("", sum(v for _, v in series)))
        return series
    return [("", _reduce([v for _, v in series], q.reduce))] if series else []


def target_summary(cfg: PrometheusConfig) -> tuple[int, int, list[str]]:
    body = _get_json(f"{cfg.base_url}/api/v1/targets?state=active", cfg.timeout_seconds)
    if body.get("status") != "success":
        raise RuntimeError("targets query failed")
    targets = (body.get("data") or {}).get("activeTargets") or []
    down = []
    for t in targets:
        if t.get("health") != "up":
            lab = t.get("labels") or {}
            down.append(f"{lab.get('job', '?')}/{lab.get('instance', '?')}"[:120])
    return len(targets) - len(down), len(targets), down[:10]


def collect(cfg: PrometheusConfig) -> PromResult:
    res = PromResult()
    if not cfg.enabled:
        return res
    queries = [q for q in cfg.queries if q.enabled]

    # 1) connectivity + target availability (one cheap call)
    try:
        if cfg.collect_targets:
            res.targets_up, res.targets_total, res.down_targets = target_summary(cfg)
        else:
            _get_json(f"{cfg.base_url}/api/v1/query?query=vector(1)", cfg.timeout_seconds)
        res.prom_status = "ok"
    except urllib.error.HTTPError as e:
        # server answered, but not usefully. Still "reachable" only if it's an API-level error
        res.prom_status = "unreachable"
        res.prom_error = f"HTTP {e.code} from Prometheus"
    except Exception as e:  # noqa: BLE001
        res.prom_status = "unreachable"
        res.prom_error = f"{e.__class__.__name__}: {getattr(e, 'reason', e)}"[:300]

    if res.prom_status != "ok":
        res.metrics_status = "failed"
        res.unavailable = [q.name for q in queries]
        return res

    # 2) metric queries
    ok = 0
    for q in queries:
        try:
            got = instant_query(cfg, q)
        except Exception as e:  # noqa: BLE001
            res.errors[q.name] = f"{e.__class__.__name__}: {e}"[:200]
            res.unavailable.append(q.name)
            continue
        if not got:
            res.unavailable.append(q.name)
        else:
            for lab, v in got:
                res.values[(q.name, lab)] = v
            ok += 1
    if not queries:
        res.metrics_status = "ok"
    elif ok == 0:
        res.metrics_status = "failed"
        res.prom_error = res.prom_error or "no configured query returned data"
    elif res.unavailable:
        res.metrics_status = "partial"
    else:
        res.metrics_status = "ok"
    return res
