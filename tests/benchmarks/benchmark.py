"""Ограниченные замеры читающих методов API; заявки и уведомления не создаются."""

import json
import math
import os
import platform
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

import httpx

from src.diagnostics import error_fields
from src.remarked import SITE, RateLimited, RemarkedClient


def _client(observation):
    def request_hook(request):
        def trace(name, info):
            # Содержимое trace info включает заголовки и токены; его не сохраняем.
            if name == "connection.connect_tcp.started":
                observation["tcp_connects"] += 1
            if name.endswith(".start_tls.started"):
                observation["tls_handshakes"] += 1

        request.extensions["trace"] = trace

    def response_hook(response):
        observation["http_status"] = response.status_code
        observation["http_version"] = response.http_version

    return httpx.Client(
        timeout=httpx.Timeout(5, connect=2), follow_redirects=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=60),
        headers={"Origin": "https://birchrestaurants.com", "Referer": SITE},
        event_hooks={"request": [request_hook], "response": [response_hook]},
    )


def measure(api, method, booking, observation, samples, **labels):
    if method not in ("GetToken", "GetBookingsSlots"):
        raise ValueError("Benchmark supports read-only methods")
    observation.clear()
    observation.update(tcp_connects=0, tls_handshakes=0)
    started = time.perf_counter()
    sample = {"method": method, "started_at": datetime.now(UTC).isoformat(),
              "started_monotonic": started, "ok": False, **labels}
    try:
        if method == "GetToken":
            api.authenticate()
        else:
            groups = api.slots(booking)
            sample["slots_count"] = sum(len(group["slots"]) for group in groups.values()) if groups else 0
        sample["ok"] = True
    except Exception as error:
        sample.update(error_fields(error))
        if isinstance(error, RateLimited):
            sample["retry_after_seconds"] = error.seconds
        raise
    finally:
        sample.update(observation)
        sample["elapsed_ms"] = (time.perf_counter() - started) * 1000
        samples.append(sample)
    return sample


def summarize(samples):
    durations = sorted(sample["elapsed_ms"] for sample in samples)
    intervals = [
        right["started_monotonic"] - left["started_monotonic"]
        for left, right in zip(samples, samples[1:])
        if left["round"] == right["round"] and left["phase"] == right["phase"]
    ]
    return {
        "requests": len(samples), "successes": sum(sample["ok"] for sample in samples),
        "http_statuses": dict(Counter(str(sample.get("http_status")) for sample in samples)),
        "min_ms": round(min(durations), 2), "p50_ms": round(median(durations), 2),
        "p95_ms": round(durations[math.ceil(len(durations) * .95) - 1], 2),
        "max_ms": round(max(durations), 2),
        "start_rate_rps": round(len(intervals) / sum(intervals), 2) if intervals and sum(intervals) else None,
        "tcp_connects": sum(sample["tcp_connects"] for sample in samples),
        "tls_handshakes": sum(sample["tls_handshakes"] for sample in samples),
    }


def _series(api, method, booking, observation, samples, *, phase, round_number, count, interval_ms,
            duration_seconds=None):
    batch = []
    deadline = time.perf_counter() + duration_seconds if duration_seconds is not None else math.inf
    for sequence in range(1, count + 1):
        if time.perf_counter() >= deadline:
            break
        sample = measure(api, method, booking, observation, samples, phase=phase,
                         round=round_number, sequence=sequence, interval_ms=interval_ms)
        batch.append(sample)
        if sequence < count:
            delay = min(interval_ms / 1000 - sample["elapsed_ms"] / 1000, deadline - time.perf_counter())
            time.sleep(max(0, delay))
    if batch:
        print(json.dumps({"phase": phase, "round": round_number, **summarize(batch)}), flush=True)


def _round(api, booking, observation, samples, round_number):
    cold = []
    for sequence in range(1, 6):
        with _client(observation) as client:
            cold.append(measure(RemarkedClient(client), "GetToken", booking, observation, samples,
                                phase="token_cold", round=round_number, sequence=sequence))
        time.sleep(.5)
    print(json.dumps({"phase": "token_cold", "round": round_number, **summarize(cold)}), flush=True)
    measure(api, "GetToken", booking, observation, samples, phase="warmup", round=round_number)
    _series(api, "GetToken", booking, observation, samples, phase="token_warm",
            round_number=round_number, count=10, interval_ms=500)
    for interval in (500, 250, 100, 50):
        time.sleep(2)
        _series(api, "GetBookingsSlots", booking, observation, samples, phase=f"slots_{interval}ms",
                round_number=round_number, count=20, interval_ms=interval)
    measure(api, "GetToken", booking, observation, samples, phase="token_before_idle", round=round_number)
    time.sleep(15)
    sample = measure(api, "GetBookingsSlots", booking, observation, samples,
                     phase="slots_after_idle_15s", round=round_number)
    print(json.dumps({"phase": "slots_after_idle_15s", "round": round_number,
                      **summarize([sample])}), flush=True)


def run(output, booking, *, sustained=False):
    """294 запроса в трёх сериях либо опрос 60 с (до 1201 запроса), до первого отказа."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    booking = {key: booking[key] for key in ("visit_date", "guests")}
    samples = []
    report = {"started_at": datetime.now(UTC).isoformat(), "booking": booking,
              "python": platform.python_version(), "platform": platform.system(),
              "httpx": httpx.__version__, "rounds_planned": 1 if sustained else 3, "samples": samples,
              "protocol": "sustained_50ms_60s" if sustained else "three_rounds",
              "proxy_environment_present": any(os.getenv(name) for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY"))}
    observation = {"tcp_connects": 0, "tls_handshakes": 0}
    try:
        with _client(observation) as client:
            api = RemarkedClient(client)
            api.verify_widget()
            report["widget_verified"] = True
            if sustained:
                measure(api, "GetToken", booking, observation, samples, phase="warmup", round=1)
                _series(api, "GetBookingsSlots", booking, observation, samples, phase="slots_50ms_sustained",
                        round_number=1, count=1200, interval_ms=50, duration_seconds=60)
            else:
                for round_number in range(1, 4):
                    _round(api, booking, observation, samples, round_number)
        report["completed"] = True
    except Exception as error:
        report.update(completed=False, stopped_by=type(error).__name__)
        if isinstance(error, RateLimited):
            report["retry_after_seconds"] = error.seconds
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["summary"] = {
            phase: summarize([sample for sample in samples if sample["phase"] == phase])
            for phase in dict.fromkeys(sample["phase"] for sample in samples)
        }
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"report": str(output), "completed": report.get("completed", False),
                          "requests": len(samples), "stopped_by": report.get("stopped_by")}), flush=True)
    return report
