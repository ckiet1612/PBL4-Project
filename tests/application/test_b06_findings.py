import pytest
from pydantic import ValidationError

from nexa.api.schemas import ResourceCapacityVector
from nexa.application.admin_service import AdminService
from nexa.application.errors import ApplicationError
from nexa.application.identity_service import IdentityService
from nexa.application.policy_service import PolicyService


def test_username_normalization_uses_casefold_equivalence() -> None:
    assert IdentityService._normalize_username("  straße ") == "strasse"
    assert AdminService._normalize_username("STRASSE") == "strasse"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("cpu_millis", 100_000_001),
        ("memory_bytes", 9_223_372_036_854_775_808),
        ("gpu_count", 2),
    ),
)
def test_resource_capacity_schema_rejects_contract_upper_bounds(field: str, value: int) -> None:
    payload = {"cpu_millis": 0, "memory_bytes": 0, "gpu_count": 0}
    payload[field] = value
    with pytest.raises(ValidationError):
        ResourceCapacityVector.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("cpu_millis", 100_000_001),
        ("memory_bytes", 9_223_372_036_854_775_808),
        ("gpu_count", 2),
    ),
)
def test_resource_capacity_service_rejects_contract_upper_bounds(field: str, value: int) -> None:
    service = PolicyService.__new__(PolicyService)
    resource = {"cpu_millis": 0, "memory_bytes": 0, "gpu_count": 0}
    resource[field] = value
    with pytest.raises(ApplicationError) as error:
        service._validated_tenant_changes({"resource_limit": resource})
    assert error.value.status == 422
