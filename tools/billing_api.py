"""Client for the public Cloud Billing Catalog API (list prices only, read-only).

This is the only GCP API the project calls. It needs an API key but no GCP credentials
and cannot change anything in a project.

Failure handling (CLAUDE.md "Rules"): every request has a timeout, transient failures are
retried with exponential backoff, each service has a total time limit, and `load_prices`
never raises because of the API. Whatever goes wrong (transport, HTTP status, malformed
JSON, an answer of a different shape), the services concerned use the static price table
and the report says so. The cause is logged, never shown.

The API key is sent in a header, not in the URL: HTTP libraries log URLs and put them in
error messages.

Scope of the live prices: the SKUs in `SERVICES` below. A SKU counts only when its
description and its unit both match, because the catalog lists different charges under
almost the same name (a "Nearline Storage" price per month, and its early-delete fee per
day).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from tools.logs import log_event, log_failure
from tools.models import Service
from tools.pricing import PriceTable, load_static_prices

BASE_URL = "https://cloudbilling.googleapis.com/v1"
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_PAGES = 50  # a broken `nextPageToken` must not loop for ever (Compute has 7 pages)
GLOBAL_REGION = "global"


@dataclass(frozen=True)
class SkuKey:
    """Which SKU gives which price: both the description and the unit must match."""

    pattern: re.Pattern[str]
    key: str  # key in the price table
    unit: str  # `usageUnit` in the catalog


@dataclass(frozen=True)
class CatalogService:
    service_id: str
    keys: tuple[SkuKey, ...]


def _keys(*rows: tuple[str, str, str]) -> tuple[SkuKey, ...]:
    return tuple(SkuKey(re.compile(pattern), key, unit) for pattern, key, unit in rows)


_HOUR, _GB_HOUR, _GB_MONTH, _SECOND, _GB_SECOND = "h", "GiBy.h", "GiBy.mo", "s", "GiBy.s"
_ONE_REGION = r"(?!.*(?:Dual-region|Multi-region|Early Delete))"

SERVICES: dict[Service, CatalogService] = {
    Service.COMPUTE: CatalogService(
        "6F81-5844-456A",
        _keys(
            (r"^E2 Instance Core running in", "compute.e2.vcpu_hour", _HOUR),
            (r"^E2 Instance Ram running in", "compute.e2.ram_gb_hour", _GB_HOUR),
            (r"^N1 Predefined Instance Core running in", "compute.n1.vcpu_hour", _HOUR),
            (r"^N1 Predefined Instance Ram running in", "compute.n1.ram_gb_hour", _GB_HOUR),
            (r"^N2 Instance Core running in", "compute.n2.vcpu_hour", _HOUR),
            (r"^N2 Instance Ram running in", "compute.n2.ram_gb_hour", _GB_HOUR),
            (r"^N2D AMD Instance Core running in", "compute.n2d.vcpu_hour", _HOUR),
            (r"^N2D AMD Instance Ram running in", "compute.n2d.ram_gb_hour", _GB_HOUR),
            (r"^Storage PD Capacity", "disk.pd-standard.gb_month", _GB_MONTH),
            (r"^Balanced PD Capacity", "disk.pd-balanced.gb_month", _GB_MONTH),
            (r"^SSD backed PD Capacity", "disk.pd-ssd.gb_month", _GB_MONTH),
        ),
    ),
    # The MySQL SKUs price every engine: PostgreSQL has the same rates, and the SQL
    # Server licence is not part of the estimate.
    Service.CLOUD_SQL: CatalogService(
        "9662-B51E-5089",
        _keys(
            (r"^Cloud SQL for MySQL: Zonal - vCPU in", "sql.vcpu_hour", _HOUR),
            (r"^Cloud SQL for MySQL: Zonal - RAM in", "sql.ram_gb_hour", _GB_HOUR),
            (r"^Cloud SQL for MySQL: Zonal - Micro instance in", "sql.db-f1-micro.hour", _HOUR),
            (r"^Cloud SQL for MySQL: Zonal - Small instance in", "sql.db-g1-small.hour", _HOUR),
            (
                r"^Cloud SQL for MySQL: Zonal - Standard storage in",
                "sql.storage.ssd.gb_month",
                _GB_MONTH,
            ),
            (
                r"^Cloud SQL for MySQL: Zonal - Low cost storage in",
                "sql.storage.hdd.gb_month",
                _GB_MONTH,
            ),
        ),
    ),
    Service.CLOUD_STORAGE: CatalogService(
        "95FF-2EF5-5EA1",
        _keys(
            (rf"^Standard Storage {_ONE_REGION}", "gcs.standard.gb_month", _GB_MONTH),
            (rf"^Nearline Storage {_ONE_REGION}", "gcs.nearline.gb_month", _GB_MONTH),
            (rf"^Coldline Storage {_ONE_REGION}", "gcs.coldline.gb_month", _GB_MONTH),
            (rf"^Archive Storage {_ONE_REGION}", "gcs.archive.gb_month", _GB_MONTH),
        ),
    ),
    Service.CLOUD_RUN: CatalogService(
        "152E-C115-5142",
        _keys(
            (
                r"^Services Min Instance CPU (?:Tier 2 )?\(Request-based billing\)",
                "run.vcpu_second_idle",
                _SECOND,
            ),
            (
                r"^Services Min Instance Memory (?:Tier 2 )?\(Request-based billing\)",
                "run.ram_gib_second_idle",
                _GB_SECOND,
            ),
            (
                r"^Services CPU \(Instance-based billing\)",
                "run.vcpu_second_always",
                _SECOND,
            ),
            (
                r"^Services Memory \(Instance-based billing\)",
                "run.ram_gib_second_always",
                _GB_SECOND,
            ),
        ),
    ),
    # One flat fee for every kind of cluster, listed under the region "global".
    Service.GKE: CatalogService(
        "CCD8-9BF1-090E",
        _keys((r"^Zonal Kubernetes Clusters$", "gke.cluster_fee_hour", _HOUR)),
    ),
}

RegionalPrices = dict[str, dict[str, float]]


class BillingApiError(Exception):
    """The Billing Catalog API could not be used. The message never contains the key."""


class BillingCatalogClient:
    """Minimal read-only client. `http`, `sleep` and `clock` are injectable for tests."""

    def __init__(
        self,
        api_key: str,
        *,
        http: httpx.Client | None = None,
        timeout_s: float = 10.0,
        total_timeout_s: float = 30.0,
        max_retries: int = 3,
        backoff_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=timeout_s)
        self._total_timeout_s = total_timeout_s
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._sleep = sleep
        self._clock = clock

    def list_skus(self, service_id: str) -> list[dict[str, Any]]:
        """Return every SKU of a service, following pagination."""
        deadline = self._clock() + self._total_timeout_s
        skus: list[dict[str, Any]] = []
        page_token = ""
        for _page in range(MAX_PAGES):
            page = self._get_page(service_id, page_token, deadline)
            skus.extend(page["skus"])
            page_token = page["nextPageToken"]
            if not page_token:
                return skus
        raise BillingApiError(f"Billing Catalog API returned more than {MAX_PAGES} pages.")

    def _get_page(self, service_id: str, page_token: str, deadline: float) -> dict[str, Any]:
        url = f"{BASE_URL}/services/{service_id}/skus"
        params = {"pageSize": "5000", "currencyCode": "USD"}
        if page_token:
            params["pageToken"] = page_token
        problem, cause = "unknown error", None
        for attempt in range(self._max_retries + 1):
            if attempt:
                self._sleep(self._backoff_s * 2 ** (attempt - 1))
            if self._clock() >= deadline:
                problem = f"no answer within {self._total_timeout_s:g} s, last: {problem}"
                break
            try:
                response = self._http.get(
                    url, params=params, headers={"X-Goog-Api-Key": self._api_key}
                )
                if response.status_code == 200:
                    return _read_page(response)
            except httpx.HTTPError as exc:
                # Never str(exc) in the message: the report shows it.
                problem, cause = type(exc).__name__, exc
                continue
            except ValueError as exc:  # malformed JSON, or JSON of another shape
                problem, cause = "malformed answer", exc
                continue
            problem = f"HTTP {response.status_code}"
            if response.status_code not in RETRY_STATUS:
                break
        raise BillingApiError(f"Billing Catalog API request failed ({problem}).") from cause


def _read_page(response: httpx.Response) -> dict[str, Any]:
    """The page's SKUs and next token. Raises `ValueError` when the shape is wrong."""
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f"expected a JSON object, got {type(body).__name__}")
    skus, token = body.get("skus", []), body.get("nextPageToken", "")
    if not isinstance(skus, list) or not isinstance(token, str):
        raise ValueError("'skus' must be a list and 'nextPageToken' a string")
    return {"skus": skus, "nextPageToken": token}


def load_prices(client: BillingCatalogClient | None) -> PriceTable:
    """Live prices where the API gives them, static prices everywhere else, with a reason.

    This is the whole boundary to the Billing API: no failure behind it gets out.
    """
    static = load_static_prices()
    if client is None:
        static.fallback_reason = "No GCP_BILLING_API_KEY configured."
        return static
    live: RegionalPrices = {}
    failed: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as pool:  # one slow API, five calls
        loading = {s: pool.submit(_service_prices, client, c) for s, c in SERVICES.items()}
    for service, future in loading.items():
        try:
            _merge(live, future.result())
        except BillingApiError as exc:
            failed[service.value] = str(exc)
            _log_fallback(service, exc)
        except Exception as exc:  # anything unexpected is still only a fallback
            failed[service.value] = f"unexpected {type(exc).__name__}"
            _log_fallback(service, exc)
    if not live:
        static.fallback_reason = "; ".join(dict.fromkeys(failed.values()))
        return static
    log_event("billing.loaded", regions=len(live), failed=sorted(failed))
    return _overlay(static, live, failed)


def _log_fallback(service: Service, exc: Exception) -> None:
    log_failure("billing.fallback", exc, level=logging.WARNING, service=service.value)


def _service_prices(client: BillingCatalogClient, catalog: CatalogService) -> RegionalPrices:
    prices = prices_from_skus(client.list_skus(catalog.service_id), catalog.keys)
    if not prices:
        raise BillingApiError("Billing Catalog API returned no usable SKUs.")
    return prices


def _merge(into: RegionalPrices, new: RegionalPrices) -> None:
    for region, prices in new.items():
        into.setdefault(region, {}).update(prices)


def _overlay(static: PriceTable, live: RegionalPrices, failed: dict[str, str]) -> PriceTable:
    """Put the live prices on top of the static ones.

    A region the API knows keeps its static exact prices for keys the API did not give.
    A global price (the GKE fee) holds in every region.
    """
    everywhere = live.pop(GLOBAL_REGION, {})
    regions = {*live, *static.regional_prices, *static.region_multipliers}
    regional = {
        region: {**static.regional_prices.get(region, {}), **everywhere, **live.get(region, {})}
        for region in regions
    }
    reason = None
    if failed:
        services = ", ".join(f"{name} ({problem})" for name, problem in failed.items())
        reason = f"Static prices were used for {services}."
    return static.model_copy(
        update={
            "source": "billing_api",
            "snapshot_date": date.today().isoformat(),
            "regional_prices": regional,
            "may_be_outdated": bool(failed),
            "fallback_reason": reason,
            "note": "Prices from the Cloud Billing Catalog API.",
        }
    )


_ALL_KEYS = tuple(key for catalog in SERVICES.values() for key in catalog.keys)


def prices_from_skus(
    skus: list[dict[str, Any]], keys: tuple[SkuKey, ...] = _ALL_KEYS
) -> RegionalPrices:
    """Turn raw SKUs into `{region: {price key: unit price}}`.

    Unknown SKUs are ignored, and so is any entry that does not have the expected shape.
    """
    regional: RegionalPrices = {}
    for sku in skus:
        try:
            found = _read_sku(sku, keys)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            continue
        if found is None:
            continue
        key, price, regions = found
        for region in regions:
            regional.setdefault(region, {})[key] = price
    return regional


def _read_sku(sku: dict[str, Any], keys: tuple[SkuKey, ...]) -> tuple[str, float, list[str]] | None:
    if sku["category"]["usageType"] != "OnDemand":
        return None
    match = next((k for k in keys if k.pattern.search(sku["description"])), None)
    if match is None:
        return None
    expression = sku["pricingInfo"][0]["pricingExpression"]
    if expression["usageUnit"] != match.unit:
        return None
    # The highest tier: the first one is sometimes a free allowance.
    price = max(_tier_price(rate["unitPrice"]) for rate in expression["tieredRates"])
    regions = [region for region in sku["serviceRegions"] if isinstance(region, str)]
    return (match.key, price, regions) if price > 0 else None


def _tier_price(unit_price: dict[str, Any]) -> float:
    return int(unit_price.get("units") or 0) + int(unit_price.get("nanos") or 0) / 1e9
