import csv
import io
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext
from fractions import Fraction
from html import escape


class ReportError(ValueError):
    """Raised when a raw comparison bundle cannot be rendered."""


def _fraction(value: str | None) -> Fraction | None:
    if value is None:
        return None
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ReportError(f"invalid rational metric: {value}") from exc


def _runs(bundle: Mapping[str, object]) -> list[dict[str, object]]:
    runs = bundle.get("runs")
    if not isinstance(runs, list) or not runs or not all(isinstance(run, dict) for run in runs):
        raise ReportError("comparison bundle requires at least one object run")
    return runs


def _nearest_rank(values: Sequence[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (percentile * len(ordered) + 99) // 100
    return ordered[rank - 1]


def _tenant_wait_p95(run: Mapping[str, object]) -> dict[str, int | None]:
    tenant_ids = [tenant["tenant_id"] for tenant in run["config"]["tenants"]]
    waits = {
        tenant_id: [
            job["wait_ms"]
            for job in run["jobs"]
            if job["tenant_id"] == tenant_id
            and job["state"] == "completed"
            and job["wait_ms"] is not None
        ]
        for tenant_id in tenant_ids
    }
    return {tenant_id: _nearest_rank(values, 95) for tenant_id, values in waits.items()}


def comparison_csv(bundle: Mapping[str, object]) -> str:
    runs = _runs(bundle)
    output = io.StringIO(newline="")
    fieldnames = [
        "seed",
        "baseline",
        "completed",
        "throughput_jobs_per_second",
        "max_wait_ms",
        "p50_wait_ms",
        "p95_wait_ms",
        "weighted_jain",
        "policy_decisions",
        "candidate_evaluations",
        "tenant_p95_wait_ms",
        "tenant_dominant_resource_time_share_ms",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for run in runs:
        writer.writerow(
            {
                "seed": run["provenance"]["seed"],
                "baseline": run["baseline"]["name"],
                "completed": run["counts"]["completed"],
                "throughput_jobs_per_second": run["metrics"]["throughput_jobs_per_second"],
                "max_wait_ms": run["metrics"]["max_wait_ms"],
                "p50_wait_ms": run["metrics"]["p50_wait_ms"],
                "p95_wait_ms": run["metrics"]["p95_wait_ms"],
                "weighted_jain": run["fairness"]["weighted_jain"],
                "policy_decisions": run["baseline"]["cost"]["policy_decisions"],
                "candidate_evaluations": run["baseline"]["cost"]["candidate_evaluations"],
                "tenant_p95_wait_ms": json.dumps(
                    _tenant_wait_p95(run), sort_keys=True, separators=(",", ":")
                ),
                "tenant_dominant_resource_time_share_ms": json.dumps(
                    run["dominant_resource_time_share_ms"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
    return output.getvalue()


def _average(values: Sequence[Fraction]) -> Fraction:
    return sum(values, Fraction(0)) / len(values) if values else Fraction(0)


def _average_optional(values: Sequence[Fraction | None]) -> Fraction | None:
    if not values or any(value is None for value in values):
        return None
    defined_values = [value for value in values if value is not None]
    return sum(defined_values, Fraction(0)) / len(defined_values)


def _attribute(value: object) -> str:
    return escape(str(value), quote=True)


def _display_fraction(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    decimal_places = 1 if abs(value) >= 1_000 else 4
    with localcontext() as context:
        context.prec = 28
        rendered = f"{Decimal(value.numerator) / Decimal(value.denominator):.{decimal_places}f}"
    return rendered.rstrip("0").rstrip(".")


def comparison_svg(bundle: Mapping[str, object]) -> str:
    runs = _runs(bundle)
    baselines = sorted({run["baseline"]["name"] for run in runs})
    tenant_ids = sorted(
        {tenant["tenant_id"] for run in runs for tenant in run["config"]["tenants"]}
    )
    baseline_colors = {
        "drr": "#2563eb",
        "drf": "#16a34a",
        "fifo": "#dc2626",
        "rr": "#9333ea",
        "wrr": "#ea580c",
    }
    tenant_palette = ("#0f766e", "#b45309", "#7e22ce", "#be123c", "#0369a1")
    tenant_colors = {
        tenant_id: tenant_palette[index % len(tenant_palette)]
        for index, tenant_id in enumerate(tenant_ids)
    }
    summaries: dict[str, dict[str, Fraction | None]] = {}
    for baseline in baselines:
        matching = [run for run in runs if run["baseline"]["name"] == baseline]
        jain_values = [_fraction(run["fairness"]["weighted_jain"]) for run in matching]
        summaries[baseline] = {
            "p95_wait_ms": _average(
                [Fraction(run["metrics"]["p95_wait_ms"] or 0) for run in matching]
            ),
            "max_wait_ms": _average(
                [Fraction(run["metrics"]["max_wait_ms"] or 0) for run in matching]
            ),
            "throughput": _average(
                [Fraction(run["metrics"]["throughput_jobs_per_second"]) for run in matching]
            ),
            "jain": _average_optional(jain_values),
        }

    panels = (
        ("P95 wait (ms)", "p95_wait_ms"),
        ("Max wait (ms)", "max_wait_ms"),
        ("Weighted Jain", "jain"),
        ("Completion / throughput", "throughput"),
    )
    summary_rows: list[tuple[str, str, Fraction, Fraction]] = []
    for baseline in baselines:
        matching = [run for run in runs if run["baseline"]["name"] == baseline]
        for tenant_id in tenant_ids:
            wait_values = [
                Fraction(wait)
                for run in matching
                if (wait := _tenant_wait_p95(run).get(tenant_id)) is not None
            ]
            resource_values = [
                Fraction(run["dominant_resource_time_share_ms"].get(tenant_id, "0"))
                for run in matching
            ]
            summary_rows.append(
                (baseline, tenant_id, _average(wait_values), _average(resource_values))
            )

    selected_seed = min(run["provenance"]["seed"] for run in runs)
    timeline_runs = sorted(
        (run for run in runs if run["provenance"]["seed"] == selected_seed),
        key=lambda run: run["baseline"]["name"],
    )
    timeline_runtime = max(run["metrics"]["simulation_runtime_ms"] for run in timeline_runs)

    width = 1000
    panel_width = 480
    panel_height = 170
    summary_start_y = 440
    timeline_start_y = summary_start_y + 58 + len(summary_rows) * 18
    height = timeline_start_y + 62 + len(timeline_runs) * 42 + 55
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="20" y="28" font-family="sans-serif" font-size="20" '
        'fill="#111827">B03 baseline simulator comparison</text>',
    ]
    for panel_index, (title, metric) in enumerate(panels):
        column = panel_index % 2
        row = panel_index // 2
        x = 20 + column * (panel_width + 10)
        y = 50 + row * (panel_height + 20)
        values = [summaries[baseline][metric] for baseline in baselines]
        defined_values = [value for value in values if value is not None]
        maximum = max(defined_values, default=Fraction(1)) or Fraction(1)
        parts.extend(
            [
                f'<rect x="{x}" y="{y}" width="{panel_width}" height="{panel_height}" '
                'fill="#f8fafc" stroke="#cbd5e1"/>',
                f'<text x="{x + 12}" y="{y + 22}" font-family="sans-serif" '
                f'font-size="14" fill="#111827">{escape(title)}</text>',
                f'<line x1="{x + 30}" y1="{y + 30}" x2="{x + 30}" y2="{y + 130}" '
                'stroke="#64748b"/>',
                f'<line x1="{x + 30}" y1="{y + 130}" x2="{x + 460}" y2="{y + 130}" '
                'stroke="#64748b"/>',
                f'<text data-kind="axis-maximum" data-exact-value="{_attribute(maximum)}" '
                f'x="{x + 26}" y="{y + 42}" text-anchor="end" '
                'font-family="sans-serif" font-size="9" fill="#475569">'
                f"{escape(_display_fraction(maximum))}</text>",
                f'<text x="{x + 26}" y="{y + 134}" text-anchor="end" '
                'font-family="sans-serif" font-size="9" fill="#475569">0</text>',
            ]
        )
        for index, baseline in enumerate(baselines):
            value = summaries[baseline][metric]
            bar_x = x + 35 + index * 86
            if value is None:
                parts.extend(
                    [
                        f'<text data-kind="undefined-metric" '
                        f'data-metric="{_attribute(metric)}" '
                        f'data-baseline="{_attribute(baseline)}" '
                        f'x="{bar_x + 31}" y="{y + 85}" text-anchor="middle" '
                        'font-family="sans-serif" font-size="12" fill="#64748b">N/A</text>',
                        f'<text x="{bar_x + 31}" y="{y + 150}" text-anchor="middle" '
                        'font-family="sans-serif" font-size="11" fill="#334155">'
                        f"{escape(baseline)}</text>",
                    ]
                )
                continue
            bar_height = int(Fraction(100) * value / maximum) if maximum else 0
            bar_y = y + 130 - bar_height
            parts.extend(
                [
                    f'<rect x="{bar_x}" y="{bar_y}" width="62" height="{bar_height}" '
                    f'fill="{baseline_colors.get(baseline, "#475569")}"/>',
                    f'<text x="{bar_x + 31}" y="{y + 150}" text-anchor="middle" '
                    'font-family="sans-serif" font-size="11" fill="#334155">'
                    f"{escape(baseline)}</text>",
                ]
            )

    parts.extend(
        [
            f'<text x="20" y="{summary_start_y}" font-family="sans-serif" font-size="16" '
            'fill="#111827">Per-tenant averages</text>',
            f'<text x="20" y="{summary_start_y + 22}" font-family="monospace" font-size="11" '
            'fill="#475569">baseline  tenant      p95 wait ms      '
            "dominant resource-time share-ms</text>",
        ]
    )
    for index, (baseline, tenant_id, wait_value, resource_value) in enumerate(summary_rows):
        y = summary_start_y + 44 + index * 18
        parts.extend(
            [
                f'<g data-kind="tenant-summary" data-baseline="{_attribute(baseline)}" '
                f'data-tenant="{_attribute(tenant_id)}" '
                f'data-p95-wait-ms="{_attribute(wait_value)}" '
                f'data-dominant-resource-time="{_attribute(resource_value)}">',
                f'<rect x="20" y="{y - 11}" width="8" height="8" '
                f'fill="{tenant_colors[tenant_id]}"/>',
                f'<text x="36" y="{y}" font-family="monospace" font-size="11" '
                f'fill="#334155">{escape(baseline):8}  {escape(tenant_id):12}  '
                f"{escape(_display_fraction(wait_value)):16}  "
                f"{escape(_display_fraction(resource_value))}</text>",
                "</g>",
            ]
        )

    axis_left = 160
    axis_right = 960
    axis_width = axis_right - axis_left
    parts.extend(
        [
            f'<text x="20" y="{timeline_start_y}" font-family="sans-serif" font-size="16" '
            f'fill="#111827">Allocation timeline (seed {selected_seed})</text>',
            f'<line x1="{axis_left}" y1="{timeline_start_y + 24}" x2="{axis_right}" '
            f'y2="{timeline_start_y + 24}" stroke="#64748b"/>',
            f'<text x="{axis_left}" y="{timeline_start_y + 18}" text-anchor="middle" '
            'font-family="sans-serif" font-size="9" fill="#475569">0 ms</text>',
            f'<text x="{axis_right}" y="{timeline_start_y + 18}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="9" fill="#475569">{timeline_runtime} ms</text>',
        ]
    )
    for row_index, run in enumerate(timeline_runs):
        baseline = run["baseline"]["name"]
        row_y = timeline_start_y + 45 + row_index * 42
        parts.extend(
            [
                f'<text x="20" y="{row_y + 10}" font-family="sans-serif" font-size="11" '
                f'fill="#334155">{escape(baseline)}</text>',
                f'<line x1="{axis_left}" y1="{row_y + 14}" x2="{axis_right}" '
                f'y2="{row_y + 14}" stroke="#cbd5e1"/>',
            ]
        )
        for allocation in run["allocation_timeline"]:
            start_ms = allocation["start_ms"]
            release_ms = allocation["release_ms"]
            bar_x = axis_left + int(Fraction(axis_width * start_ms, timeline_runtime))
            bar_end = axis_left + int(Fraction(axis_width * release_ms, timeline_runtime))
            bar_width = max(1, bar_end - bar_x)
            tenant_id = allocation["tenant_id"]
            parts.append(
                f'<rect data-kind="allocation-interval" '
                f'data-baseline="{_attribute(baseline)}" '
                f'data-tenant="{_attribute(tenant_id)}" '
                f'data-job-id="{_attribute(allocation["job_id"])}" '
                f'data-start-ms="{start_ms}" data-release-ms="{release_ms}" '
                f'x="{bar_x}" y="{row_y}" width="{bar_width}" height="12" '
                f'fill="{tenant_colors[tenant_id]}" fill-opacity="0.58"/>'
            )
    parts.append("</svg>\n")
    return "\n".join(parts)
