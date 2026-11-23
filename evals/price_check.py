"""How far are the static prices from today's list prices?

    uv run python -m evals.price_check      # free; needs GCP_BILLING_API_KEY

The cost-error metric compares the estimator with sums made by hand from the same static
table, so it measures arithmetic, not whether the prices are right. This compares the
table itself with the live Billing Catalog, price by price and region by region, and
estimates the dataset samples with both.
"""

from __future__ import annotations

import os
import statistics
import sys
from typing import Any

from dotenv import load_dotenv

from evals.dataset_loader import Sample, default_region, load_samples
from tools.billing_api import BillingCatalogClient, load_prices
from tools.cost_estimator import estimate_cost
from tools.pricing import PriceTable, load_static_prices
from tools.tf_parser import parse_terraform


def _error_pct(static: float, live: float) -> float:
    return round(100 * (static - live) / live, 2)


def compare_tables(static: PriceTable, live: PriceTable) -> list[dict[str, Any]]:
    """One row per price and region the static table lists."""
    return [
        {
            "key": key,
            "service": key.split(".")[0],
            "region": region,
            "static": static.unit_price(key, region),
            "live": live.unit_price(key, region),
            "error_pct": _error_pct(static.unit_price(key, region), live.unit_price(key, region)),
        }
        for key in sorted(static.prices)
        for region in static.region_multipliers
    ]


def compare_samples(
    samples: list[Sample], static: PriceTable, live: PriceTable
) -> list[dict[str, Any]]:
    rows = []
    for sample in samples:
        if not sample.expected_cost_usd:  # no expected cost, or a true zero
            continue
        resources, region = parse_terraform(sample.terraform), default_region()
        static_usd = estimate_cost(resources, region, static).total_monthly_usd
        live_usd = estimate_cost(resources, region, live).total_monthly_usd
        rows.append(
            {
                "file": sample.file,
                "static_usd": static_usd,
                "live_usd": live_usd,
                "error_pct": _error_pct(static_usd, live_usd),
            }
        )
    return rows


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per service: how many prices are off, and by how much."""
    result = []
    for service in dict.fromkeys(row["service"] for row in rows):
        errors = [abs(row["error_pct"]) for row in rows if row["service"] == service]
        result.append(
            {
                "service": service,
                "n_prices": len(errors),
                "n_off_by_over_1_pct": sum(1 for error in errors if error > 1),
                "mean_abs_error_pct": round(statistics.fmean(errors), 2),
                "max_abs_error_pct": max(errors),
            }
        )
    return result


def main() -> int:
    from evals.run import metadata, save

    load_dotenv()
    key = os.getenv("GCP_BILLING_API_KEY", "").strip()
    static = load_static_prices()
    live = load_prices(BillingCatalogClient(key) if key else None)
    if live.source != "billing_api" or live.fallback_reason:
        print(f"Live prices are not complete: {live.fallback_reason}")
        return 1
    rows = compare_tables(static, live)
    path = save(
        "price_check",
        {
            "meta": {**metadata("price_check"), "static_snapshot": static.snapshot_date},
            "summary": summarize(rows),
            "samples": compare_samples(load_samples(), static, live),
            "prices": rows,
        },
    )
    print(f"Saved to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
