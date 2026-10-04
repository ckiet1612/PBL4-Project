"""B19 metrics without a database: registry allowlist, cardinality, Jain, after-commit."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from nexa.observability import metrics as common
from nexa.observability import metrics_api as api
from nexa.observability.fairness import jain_index

ID_LIKE = ("-", "@", "/v1/jobs/0")


@given(st.lists(st.floats(min_value=1e-6, max_value=1e9), min_size=1, max_size=60))
def test_jain_is_within_bounds(values):
    index = jain_index(values)
    assert 1 / len(values) - 1e-12 <= index <= 1.0


@given(st.floats(min_value=1e-6, max_value=1e9), st.integers(min_value=1, max_value=60))
def test_jain_is_one_when_equal(value, count):
    assert jain_index([value] * count) == pytest.approx(1.0)


def test_jain_ignores_idle_tenants_and_handles_none():
    assert jain_index([]) is None
    assert jain_index([0, 0]) is None
    assert jain_index([1, 0, 1]) == pytest.approx(1.0)
    assert jain_index([1, 0, 0, 0]) == pytest.approx(1.0)
    assert jain_index([3, 1]) == pytest.approx(16 / 20)


def test_closed_labels_map_unknown_values():
    assert common.closed("GET", common.HTTP_METHODS, "OTHER") == "GET"
    assert common.closed("BREW", common.HTTP_METHODS, "OTHER") == "OTHER"
    assert common.closed(None, ("a",), "none") == "none"
    assert api.status_class(204) == "2xx"
    assert api.status_class(999) == "5xx"


def _snapshot(registry):
    return {
        (family.name, tuple(sorted(sample.labels.items())))
        for family in registry.collect()
        for sample in family.samples
    }


def _drive(tenants: int, jobs: int):
    """Emit the same label combinations whatever the tenant/job counts."""
    for _tenant in range(tenants):
        for _job in range(jobs):
            api.observe_http("/v1/jobs/{job_id}", "GET", 200, 0.01)
            api.observe_http("/v1/jobs", "POST", 202, 0.02)
            api.admission("job", "accepted")
            api.admission("job", "rejected", "quota_exceeded")
            api.storage_rejection("high_watermark", "admission")
            api.retry_scheduled("INFRASTRUCTURE")
            api.restore("selected")


def test_series_count_does_not_grow_with_tenants_or_jobs():
    _drive(1, 1)
    small = _snapshot(api.REGISTRY)
    _drive(50, 500)
    large = _snapshot(api.REGISTRY)
    assert small == large


def test_registry_labels_are_allowlisted_and_never_ids():
    api.callback_rejected("/v1/internal/attempts/{attempt_id}/renew", "stale_authority")
    api.callback_rejected("/v1/internal/attempts/{attempt_id}/renew", "unknown_code")
    api.admission("bogus", "bogus", "message text with spaces")
    api.storage_rejection("disk on fire", "elsewhere")
    allowed = {
        "route",
        "method",
        "status_class",
        "kind",
        "outcome",
        "reason",
        "operation",
        "code",
        "level",
        "check",
        "collector",
        "le",
    }
    values = common.label_values(api.REGISTRY)
    assert set(values) <= allowed
    reasons = api.ERROR_CODES | set(api.STORAGE_REJECTION_REASONS) | set(api.FAILURE_CLASSES)
    assert values["reason"] <= reasons | {"none"}
    assert values["code"] <= set(api.CALLBACK_REJECTION_CODES)
    for name, label_set in values.items():
        if name == "route":
            assert all(value.startswith("/v1/") or value == "unmatched" for value in label_set)
            continue
        for value in label_set:
            assert not any(marker in value for marker in ID_LIKE), (name, value)


def test_guarded_swallows_and_counts_failures():
    before = common.INTERNAL_ERRORS._value.get()

    def boom():
        raise RuntimeError("x")

    common.guarded(boom)
    assert common.INTERNAL_ERRORS._value.get() == before + 1


def test_after_commit_runs_only_on_commit_and_respects_savepoints():
    engine = create_engine("sqlite://")
    fired: list[str] = []
    session = Session(engine)
    with session.begin():
        session.execute(text("select 1"))
        common.after_commit(session, lambda: fired.append("outer"))
        try:
            with session.begin_nested():
                common.after_commit(session, lambda: fired.append("rolled-back savepoint"))
                raise ValueError
        except ValueError:
            pass
        with session.begin_nested():
            common.after_commit(session, lambda: fired.append("released savepoint"))
        assert fired == []
    assert fired == ["outer", "released savepoint"]

    fired.clear()
    try:
        with session.begin():
            common.after_commit(session, lambda: fired.append("never"))
            raise ValueError
    except ValueError:
        pass
    with session.begin():
        pass
    assert fired == []


def test_after_commit_failure_is_counted_not_raised():
    engine = create_engine("sqlite://")
    before = common.INTERNAL_ERRORS._value.get()
    session = Session(engine)
    with session.begin():
        common.after_commit(session, lambda: 1 / 0)
    assert common.INTERNAL_ERRORS._value.get() == before + 1


def test_exposition_is_text_format():
    payload = common.exposition(api.REGISTRY)
    assert b"# TYPE nexa_http_requests_total counter" in payload
    assert b"nexa_metrics_internal_errors_total" in payload
    assert b"process_cpu" not in payload and b"python_gc" not in payload


def test_closed_counters_are_exposed_at_zero_from_import():
    # B19-RV03: increase()/rate() miss the first event of a series that appears at 1.
    import subprocess
    import sys

    script = (
        "from nexa.observability import metrics_api, metrics_coordinator, metrics_worker\n"
        "from nexa.observability.metrics import exposition\n"
        "for module in (metrics_api, metrics_coordinator, metrics_worker):\n"
        "    print(exposition(module.REGISTRY).decode())\n"
    )
    output = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    ).stdout
    assert 'nexa_restore_total{outcome="failed"} 0.0' in output
    for outcome in api.RESTORE_OUTCOMES:
        assert f'nexa_restore_total{{outcome="{outcome}"}} 0.0' in output
    assert 'nexa_gc_errors_total{kind="reconcile"} 0.0' in output
    assert 'nexa_retry_scheduled_total{reason="OOM"} 0.0' in output
    assert 'nexa_storage_rejections_total{operation="worker_upload",reason="enospc"} 0.0' in output
    assert 'nexa_coordinator_maintenance_failures_total{step="reap"} 0.0' in output
    assert 'nexa_worker_executions_total{outcome="OOM"} 0.0' in output
    assert 'nexa_worker_loop_failures_total{operation="heartbeat"} 0.0' in output


def test_every_increase_or_rate_alert_uses_a_preinitialised_counter():
    import re
    from pathlib import Path

    rules = Path("deploy/prometheus/alerts.yml").read_text()
    used = set(re.findall(r"\b(?:increase|rate|irate|delta)\(\s*([a-z_:]+)", rules))
    preinitialised = {
        "nexa_restore_total",
        "nexa_gc_errors_total",
        "nexa_gc_deleted_total",
        "nexa_gc_bytes_deleted_total",
        "nexa_checksum_errors_total",
        "nexa_retry_scheduled_total",
        "nexa_storage_rejections_total",
        "nexa_collector_errors_total",
        "nexa_coordinator_decisions_total",
        "nexa_coordinator_maintenance_failures_total",
        "nexa_coordinator_quota_blocked_total",
        "nexa_worker_executions_total",
        "nexa_worker_loop_failures_total",
    }
    assert used and used <= preinitialised, used - preinitialised
