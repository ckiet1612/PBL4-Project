"""Remediation B15-R33: a rejected stale callback is visible as one bounded JSON log line.

A ``409 stale_authority`` commits nothing, so it is not a recovery event
(``RECOVERY_EVENT_TYPES`` is closed and unchanged). The API logs the rejection at
WARNING with the route template, fixed message, request ID and the path identities; never
the credential, the callback body or the authority.
"""

import json
import logging

import pytest

from tests.integration.test_control_b15 import Control

pytestmark = pytest.mark.postgres

LOGGER = "nexa.api.app"


def _renew(control, authority):
    return control.job.post(
        "/renew", {"authority": authority, "progress_sequence": 0, "progress": None}
    )


def test_a_stale_callback_rejection_is_logged_once_without_secrets(
    migrated_postgres_engine, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(logging.getLogger(LOGGER), "disabled", False)
    with Control(migrated_postgres_engine, tmp_path, label="rem-r33") as control:
        authority = control.job.authority
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            stale = _renew(control, {**authority, "job_fence": authority["job_fence"] + 1})
            live = _renew(control, authority)
            missing = control.job.client.post(
                "/v1/attempts/0198a000-0000-7000-8000-000000000009/renew",
                headers={"Authorization": f"Bearer {control.job.credential}"},
                json={"authority": authority, "progress_sequence": 0, "progress": None},
            )
        assert (stale.status_code, stale.json()["code"]) == (409, "stale_authority")
        assert live.status_code == 200, live.text
        assert missing.status_code != 409
        lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == LOGGER]
        assert lines == [
            {
                "event": "worker_callback_rejected",
                "method": "POST",
                "route": "/v1/attempts/{attempt_id}/renew",
                "status": 409,
                "code": "stale_authority",
                "message": stale.json()["message"],
                "request_id": stale.headers["X-Request-Id"],
                "attempt_id": authority["attempt_id"],
            }
        ]
        assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.WARNING]
        for secret in (control.job.credential, authority["lease_id"], authority["allocation_id"]):
            assert secret not in caplog.text
