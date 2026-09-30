"""Make every Nexa SQL function independent of the caller's search_path (B13-R12).

PostgreSQL 17 evaluates expression indexes and CHECK constraints during ANALYZE, autoanalyze,
CREATE INDEX and REINDEX under a restricted search_path, and pg_restore loads data with an empty
one. The B05 Decimal helpers called each other unqualified, so ``ANALYZE fairness_ledgers`` failed
on ``ix_fairness_ledgers_score`` and a plain pg_dump/pg_restore failed on the Decimal CHECKs.

The six Decimal helpers that call another helper are replaced with bodies whose only change is
that those calls are qualified with the schema the functions actually live in (read from
``pg_proc``, never assumed to be ``public``). They keep no SET clause, so the SQL-language ones stay
inlinable, and their results are unchanged, so the expression index and the validated CHECKs need
no REINDEX or re-validation. Every trigger or helper function that references Nexa tables or
functions gets ``SET search_path = pg_catalog, <its schema>, pg_temp``. Downgrade restores the B05
bodies byte for byte and resets the pinned search_path.
"""

from alembic import context, op
from sqlalchemy import text

from nexa.infrastructure.persistence.schema_v18 import (
    QUALIFIED_DECIMAL_FUNCTIONS,
    SEARCH_PATH_PINNED_FUNCTIONS,
)

revision = "20260928_0021"
down_revision = "20260928_0020"
branch_labels = None
depends_on = None

_SENTINEL_FUNCTION = "nexa_decimal_is_valid(text)"
_QUALIFIER = "{q}"

# Header and body of each helper exactly as migration 20260919_0001 created them, with every call
# to another Decimal helper prefixed by the qualifier placeholder. Rendering with an empty
# qualifier reproduces the B05 bodies byte for byte (asserted by the remediation tests).
_DECIMAL_DEFINITIONS: dict[str, tuple[str, str]] = {
    "nexa_decimal_is_nonnegative(text)": (
        "nexa_decimal_is_nonnegative(encoded text) RETURNS boolean\n"
        "        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE",
        """
          SELECT {q}nexa_decimal_is_valid(encoded)
            AND (split_part(encoded, ':', 1) = '0' OR split_part(encoded, ':', 2) = '0')
        """,
    ),
    "nexa_decimal_is_positive(text)": (
        "nexa_decimal_is_positive(encoded text) RETURNS boolean\n"
        "        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE",
        """
          SELECT {q}nexa_decimal_is_valid(encoded)
            AND split_part(encoded, ':', 1) = '0'
            AND split_part(encoded, ':', 2) <> '0'
        """,
    ),
    "nexa_decimal_zero_rank(text)": (
        "nexa_decimal_zero_rank(encoded text) RETURNS integer\n"
        "        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE",
        """
        BEGIN
          IF NOT {q}nexa_decimal_is_valid(encoded) THEN
            RAISE EXCEPTION 'invalid exact decimal encoding'
              USING ERRCODE = '23514';
          END IF;
          IF split_part(encoded, ':', 2) = '0' THEN
            RETURN 0;
          END IF;
          RETURN 1;
        END
        """,
    ),
    "nexa_decimal_adjusted_exponent(text)": (
        "nexa_decimal_adjusted_exponent(encoded text) RETURNS numeric\n"
        "        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE",
        """
        DECLARE digits text;
        BEGIN
          IF NOT {q}nexa_decimal_is_valid(encoded) THEN
            RAISE EXCEPTION 'invalid exact decimal encoding'
              USING ERRCODE = '23514';
          END IF;
          digits := split_part(encoded, ':', 2);
          IF digits = '0' THEN
            RETURN 0;
          END IF;
          RETURN split_part(encoded, ':', 3)::numeric + length(digits) - 1;
        END
        """,
    ),
    "nexa_decimal_normalized_significand(text)": (
        "nexa_decimal_normalized_significand(encoded text) RETURNS text\n"
        "        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE",
        """
        DECLARE digits text;
        DECLARE normalized text;
        BEGIN
          IF NOT {q}nexa_decimal_is_valid(encoded) THEN
            RAISE EXCEPTION 'invalid exact decimal encoding'
              USING ERRCODE = '23514';
          END IF;
          digits := split_part(encoded, ':', 2);
          IF digits = '0' THEN
            RETURN '0';
          END IF;
          normalized := rtrim(digits, '0');
          RETURN normalized;
        END
        """,
    ),
    "nexa_decimal_compare(text, text)": (
        "nexa_decimal_compare(left_value text, right_value text) RETURNS integer\n"
        "        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE",
        """
        DECLARE left_sign integer;
        DECLARE right_sign integer;
        DECLARE left_zero boolean;
        DECLARE right_zero boolean;
        DECLARE left_exponent numeric;
        DECLARE right_exponent numeric;
        DECLARE left_significand text;
        DECLARE right_significand text;
        DECLARE magnitude_comparison integer;
        BEGIN
          IF NOT {q}nexa_decimal_is_valid(left_value)
             OR NOT {q}nexa_decimal_is_valid(right_value) THEN
            RAISE EXCEPTION 'invalid exact decimal encoding'
              USING ERRCODE = '23514';
          END IF;
          left_sign := split_part(left_value, ':', 1)::integer;
          right_sign := split_part(right_value, ':', 1)::integer;
          left_zero := split_part(left_value, ':', 2) = '0';
          right_zero := split_part(right_value, ':', 2) = '0';
          IF left_zero AND right_zero THEN
            RETURN 0;
          END IF;
          IF left_zero THEN
            IF right_sign = 1 THEN
              RETURN 1;
            END IF;
            RETURN -1;
          END IF;
          IF right_zero THEN
            IF left_sign = 1 THEN
              RETURN -1;
            END IF;
            RETURN 1;
          END IF;
          IF left_sign <> right_sign THEN
            IF left_sign = 1 THEN
              RETURN -1;
            END IF;
            RETURN 1;
          END IF;

          left_exponent := {q}nexa_decimal_adjusted_exponent(left_value);
          right_exponent := {q}nexa_decimal_adjusted_exponent(right_value);
          left_significand := {q}nexa_decimal_normalized_significand(left_value);
          right_significand := {q}nexa_decimal_normalized_significand(right_value);
          IF left_exponent < right_exponent THEN
            magnitude_comparison := -1;
          ELSIF left_exponent > right_exponent THEN
            magnitude_comparison := 1;
          ELSIF left_significand COLLATE "C" < right_significand COLLATE "C" THEN
            magnitude_comparison := -1;
          ELSIF left_significand COLLATE "C" > right_significand COLLATE "C" THEN
            magnitude_comparison := 1;
          ELSE
            magnitude_comparison := 0;
          END IF;
          IF left_sign = 1 THEN
            RETURN -magnitude_comparison;
          END IF;
          RETURN magnitude_comparison;
        END
        """,
    ),
}

if set(_DECIMAL_DEFINITIONS) != set(QUALIFIED_DECIMAL_FUNCTIONS):
    raise RuntimeError("20260928_0021 must replace exactly the qualified Decimal helpers")


def render_decimal_body(signature: str, qualifier: str) -> str:
    """Return the body of ``signature`` with ``qualifier`` in front of every helper call."""
    return _DECIMAL_DEFINITIONS[signature][1].replace(_QUALIFIER, qualifier)


def _quote(identifier: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote_identifier(identifier)


def _schema_of(signature: str) -> str:
    return (
        op.get_bind()
        .execute(
            text(
                "SELECT n.nspname FROM pg_catalog.pg_proc p "
                "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
                "WHERE p.oid = CAST(:signature AS pg_catalog.regprocedure)"
            ),
            {"signature": signature},
        )
        .scalar_one()
    )


def _replace_decimal_functions(*, qualified: bool) -> None:
    schema = _quote(_schema_of(_SENTINEL_FUNCTION))
    qualifier = f"{schema}." if qualified else ""
    for signature, (header, _) in _DECIMAL_DEFINITIONS.items():
        if _schema_of(signature) != _schema_of(_SENTINEL_FUNCTION):
            raise RuntimeError(f"{signature} is not in the schema of {_SENTINEL_FUNCTION}")
        body = render_decimal_body(signature, qualifier)
        if "$body$" in body:
            raise RuntimeError("Decimal function body contains the dollar-quote tag")
        op.execute(f"CREATE OR REPLACE FUNCTION {schema}.{header} AS $body${body}$body$")


def _set_pinned_search_path(*, pinned: bool) -> None:
    for signature in SEARCH_PATH_PINNED_FUNCTIONS:
        schema = _quote(_schema_of(signature))
        target = f"{schema}.{signature}"
        if pinned:
            op.execute(f"ALTER FUNCTION {target} SET search_path = pg_catalog, {schema}, pg_temp")
        else:
            op.execute(f"ALTER FUNCTION {target} RESET search_path")


def _require_online() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "20260928_0021 reads the function schema from pg_proc and needs an online migration"
        )


def upgrade() -> None:
    _require_online()
    _replace_decimal_functions(qualified=True)
    _set_pinned_search_path(pinned=True)


def downgrade() -> None:
    _require_online()
    _set_pinned_search_path(pinned=False)
    _replace_decimal_functions(qualified=False)
