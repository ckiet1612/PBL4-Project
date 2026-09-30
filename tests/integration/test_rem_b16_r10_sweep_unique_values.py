"""B16-R10: a repeated value in one sweep dimension is 422; a stored sweep replays unchanged.

`SweepDimension.values` is `uniqueItems` (OpenAPI), compared by RFC 8785 form, so
`0.01` and `1e-2` are one value. A new request with a repeat is rejected and
persists nothing. A sweep stored before the rule keeps its immutable mapping: a
replay of its key returns it and resumes its unfinished indexes from the stored
expansion, without validating the request again.
"""

import json

import pytest
from sqlalchemy import select

from nexa.application import sweep_expansion
from nexa.infrastructure.persistence import schema as s
from tests.api.test_http_contract import _client
from tests.integration.test_sweep_b16 import (
    _admission_state,
    _count,
    _crash_after,
    _crashed_post,
    _dataset,
    _members,
    _post,
    _request,
)
from tests.integration.test_templates_b16 import _register_all

pytestmark = pytest.mark.postgres

REPEATED = [
    {"name": "learning_rate", "values": [0.1, 0.01, 0.01, 0.001]},
    {"name": "seed", "values": [1, 2]},
]


def _raw_post(client, headers, tenant_id, key, raw):
    """Post JSON text as written, so number spellings such as `1e-2` reach the server."""
    return client.post(
        "/v1/sweeps",
        headers={
            **headers,
            "X-Nexa-Tenant-Id": tenant_id,
            "Idempotency-Key": key,
            "Content-Type": "application/json",
        },
        content=raw,
    )


def _nothing_persisted(engine, tenant_id):
    assert _count(engine, s.sweep_parents) == 0
    assert _count(engine, s.jobs) == 0
    assert (
        _count(
            engine,
            s.idempotency_records,
            s.idempotency_records.c.operation_id.in_(["submitSweep", "submitJob"]),
        )
        == 0
    )
    counters, _buckets = _admission_state(engine, tenant_id)
    assert all(outstanding == 0 for _scope, outstanding, _v in counters)


def test_a_repeated_value_or_name_is_rejected_and_persists_nothing(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        artifact_id = _dataset(engine, tenant_id)
        spellings = json.dumps(
            _request(artifact_id, [{"name": "learning_rate", "values": [0.01, "SPELLING"]}])
        )
        cases = [
            (_request(artifact_id, REPEATED), "values must be unique"),
            (
                _request(artifact_id, [{"name": "seed", "values": [1, 2, 1]}]),
                "values must be unique",
            ),
            (
                _request(
                    artifact_id,
                    [{"name": "seed", "values": [1]}, {"name": "seed", "values": [1]}],
                ),
                "names must be unique",
            ),
        ]
        responses = [
            _post(client, write, tenant_id, f"rem-r10-reject-{index:04d}", body)
            for index, (body, _message) in enumerate(cases)
        ]
        for spelling in ("1e-2", "0.010", "1.0E-2"):
            raw = spellings.replace('"SPELLING"', spelling)
            responses.append(_raw_post(client, write, tenant_id, f"rem-r10-raw-{spelling}", raw))
            cases.append((None, "values must be unique"))
        for response, (_body, message) in zip(responses, cases, strict=True):
            assert response.status_code == 422, response.text
            assert response.json()["code"] == "validation_failed"
            assert message in response.json()["message"]
        _nothing_persisted(engine, tenant_id)

        # The key was not spent: the same key with unique values is a normal sweep.
        fixed = _post(client, write, tenant_id, "rem-r10-reject-0000", _request(artifact_id))
        assert fixed.status_code == 207, fixed.text
        assert fixed.json()["child_count"] == 6


def _without_the_uniqueness_rule(monkeypatch):
    """The expansion as stored before B16-R10: repeats were dropped, not rejected."""
    original = sweep_expansion._check_shape

    def check_shape(dimensions):
        original([(name, sweep_expansion.canonical_values(values)) for name, values in dimensions])

    monkeypatch.setattr(sweep_expansion, "_check_shape", check_shape)


def test_a_sweep_stored_before_the_rule_replays_and_resumes_unchanged(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        body = _request(_dataset(engine, tenant_id), REPEATED)
        _without_the_uniqueness_rule(monkeypatch)
        _crash_after(monkeypatch, 2)
        _crashed_post(client, write, tenant_id, "rem-r10-stored-0001", body)
        monkeypatch.undo()
        with engine.connect() as connection:
            parent = connection.execute(select(s.sweep_parents)).mappings().one()
            before = connection.execute(
                select(s.sweep_children.c.child_index, s.sweep_children.c.job_id).order_by(
                    s.sweep_children.c.child_index
                )
            ).all()
        assert (parent["child_count"], parent["accepted_count"]) == (6, 2)

        # The rule is back. Replay is answered from the stored record, before validation.
        resumed = _post(client, write, tenant_id, "rem-r10-stored-0001", body)
        assert resumed.status_code == 207, resumed.text
        sweep = resumed.json()
        assert sweep["sweep_id"] == str(parent["sweep_id"])
        children = sweep["children"]
        assert [child["child_index"] for child in children] == list(range(6))
        assert {child["status"] for child in children} == {"ACCEPTED"}
        assert [child["job_id"] for child in children[:2]] == [str(job) for _i, job in before]
        unique = [
            (item["name"], sweep_expansion.canonical_values(item["values"])) for item in REPEATED
        ]
        expected = [
            sweep_expansion.parameter_hash(child)
            for child in sweep_expansion.expand(body["base_spec"]["parameters"], unique)
        ]
        assert [child["parameter_hash"] for child in children] == expected
        assert _count(engine, s.jobs) == 6

        replay = _post(client, write, tenant_id, "rem-r10-stored-0001", body)
        assert replay.status_code == 207, replay.text
        assert replay.json() == sweep
        assert _count(engine, s.jobs) == 6

        # Only a new request is validated.
        fresh = _post(client, write, tenant_id, "rem-r10-stored-0002", body)
        assert fresh.status_code == 422, fresh.text
        assert fresh.json()["code"] == "validation_failed"
        assert _count(engine, s.sweep_parents) == 1
