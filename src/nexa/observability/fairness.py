"""System-wide fairness summary exposed as a metric (B19-R04): no per-tenant series."""

from collections.abc import Iterable


def jain_index(values: Iterable[float]) -> float | None:
    """Jain's index (Σx)² / (n·Σx²) over tenants with service; None when nobody ran.

    The value lies in [1/n, 1] and equals 1 exactly when every tenant got the same
    normalized service.
    """
    shares = [float(value) for value in values if value > 0]
    if not shares:
        return None
    total = sum(shares)
    squares = sum(value * value for value in shares)
    index = (total * total) / (len(shares) * squares)
    # Clamp float rounding so the documented bounds hold exactly.
    return min(1.0, max(1.0 / len(shares), index))
