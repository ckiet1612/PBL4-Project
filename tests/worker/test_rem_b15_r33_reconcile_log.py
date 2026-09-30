"""Remediation B15-R33: a reconcile outcome is visible as one bounded JSON log line.

Reconcile outcomes commit nothing, so they are not recovery events
(`RECOVERY_EVENT_TYPES` is closed). The worker logs an incomplete scan, a scan that
adopted an attempt, and the first complete scan after start or after an incomplete one;
a steady healthy scan stays silent. The line holds counts only.
"""

import json
import logging

from nexa.worker.agent import WorkerAgent

LOGGER = "nexa.worker.agent"


class Client:
    def reconciliation(self, *_args, **_kwargs):
        return {"items": [], "page": {"next_cursor": None}}


def _lines(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == LOGGER]


def test_reconcile_outcomes_are_logged_as_bounded_json(monkeypatch, caplog):
    monkeypatch.setattr(logging.getLogger(LOGGER), "disabled", False)
    agent = WorkerAgent.for_test(Client(), worker_id="worker", incarnation_id="incarnation")
    pending = ["0198a000-0000-7000-8000-000000000001"]
    monkeypatch.setattr(agent, "_blocking_pending_attempts", lambda: list(pending))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert agent.reconcile_once().complete is False
        assert agent.reconcile_once().complete is False
        pending.clear()
        assert agent.reconcile_once().complete is True
        assert agent.reconcile_once().complete is True
    outcome = {"event": "worker_reconcile", "items_seen": 0, "adopted": 0}
    assert _lines(caplog) == [
        {**outcome, "complete": False, "unresolved": 1},
        {**outcome, "complete": False, "unresolved": 1},
        {**outcome, "complete": True, "unresolved": 0},
    ]
    assert pending == [] and "0198a000" not in caplog.text


def test_the_first_complete_scan_after_start_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(logging.getLogger(LOGGER), "disabled", False)
    agent = WorkerAgent.for_test(Client(), worker_id="worker", incarnation_id="incarnation")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        agent.reconcile_once()
        agent.reconcile_once()
    assert _lines(caplog) == [
        {
            "event": "worker_reconcile",
            "items_seen": 0,
            "adopted": 0,
            "complete": True,
            "unresolved": 0,
        }
    ]
