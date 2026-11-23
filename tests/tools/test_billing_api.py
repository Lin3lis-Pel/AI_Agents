import httpx
import pytest

from tools.billing_api import (
    SERVICES,
    BillingApiError,
    BillingCatalogClient,
    load_prices,
    prices_from_skus,
)
from tools.models import Service

API_KEY = "test-key-do-not-leak"


def _sku(
    description: str, nanos: int, regions: list[str], usage: str = "OnDemand", unit: str = "h"
) -> dict:
    return {
        "description": description,
        "category": {"usageType": usage},
        "serviceRegions": regions,
        "pricingInfo": [
            {
                "pricingExpression": {
                    "usageUnit": unit,
                    "tieredRates": [{"unitPrice": {"units": "0", "nanos": nanos}}],
                }
            }
        ],
    }


SKUS = [
    _sku("E2 Instance Core running in Americas", 22_000_000, ["us-central1", "us-east1"]),
    _sku("E2 Instance Ram running in Americas", 3_000_000, ["us-central1"], unit="GiBy.h"),
    _sku("Spot Preemptible E2 Instance Core running in Americas", 6_000_000, ["us-central1"]),
    _sku("E2 Instance Core running in Americas", 9_000_000, ["us-central1"], usage="Commit1Yr"),
    _sku("Balanced PD Capacity", 110_000_000, ["europe-west1"], unit="GiBy.mo"),
    _sku("Some unrelated SKU", 5, ["us-central1"]),
]


# One or two SKUs per service, with descriptions and units as the real catalog has them
# (read on 2026-11-23).
CATALOG = {
    Service.COMPUTE: SKUS,
    Service.CLOUD_SQL: [
        _sku("Cloud SQL for MySQL: Zonal - vCPU in Americas", 41_300_000, ["us-central1"]),
        _sku("Cloud SQL for MySQL: Regional - vCPU in Americas", 82_600_000, ["us-central1"]),
    ],
    Service.CLOUD_STORAGE: [
        _sku("Nearline Storage Iowa", 10_000_000, ["us-central1"], unit="GiBy.mo"),
        _sku("Nearline Storage Iowa Dual-region", 11_000_000, ["us-central1"], unit="GiBy.mo"),
        _sku("Nearline Storage Iowa (Early Delete)", 333_330, ["us-central1"], unit="GiBy.d"),
    ],
    Service.CLOUD_RUN: [
        _sku("Services Min Instance CPU (Request-based billing)", 2_500, ["us-central1"], unit="s"),
        _sku(
            "Services Min Instance CPU Tier 2 (Request-based billing)",
            3_500,
            ["europe-west3"],
            unit="s",
        ),
    ],
    Service.GKE: [_sku("Zonal Kubernetes Clusters", 100_000_000, ["global"])],
}


def _catalog_handler(request: httpx.Request, broken: dict | None = None) -> httpx.Response:
    """Answer like the Billing Catalog: each service has its own SKUs. `broken` maps a
    service to the response it should give instead."""
    service = next(s for s, c in SERVICES.items() if c.service_id in request.url.path)
    if broken and service in broken:
        return broken[service]
    return httpx.Response(200, json={"skus": CATALOG[service]})


def _client(handler, **kwargs) -> BillingCatalogClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return BillingCatalogClient(API_KEY, http=http, sleep=lambda _: None, **kwargs)


def test_skus_become_regional_prices_and_ignore_spot_and_commitments():
    regional = prices_from_skus(SKUS)

    assert regional["us-central1"] == {
        "compute.e2.vcpu_hour": pytest.approx(0.022),
        "compute.e2.ram_gb_hour": pytest.approx(0.003),
    }
    assert regional["us-east1"] == {"compute.e2.vcpu_hour": pytest.approx(0.022)}
    assert regional["europe-west1"] == {"disk.pd-balanced.gb_month": pytest.approx(0.11)}


def test_live_prices_override_static_ones_and_are_not_marked_outdated():
    prices = load_prices(_client(_catalog_handler))

    assert prices.source == "billing_api"
    assert prices.may_be_outdated is False
    assert prices.fallback_reason is None
    assert prices.unit_price("compute.e2.vcpu_hour", "us-central1") == pytest.approx(0.022)
    # a SKU the API did not return: still served from the static table
    assert prices.unit_price("sql.ram_gb_hour", "us-central1") == pytest.approx(0.007)


def test_all_five_services_have_live_prices():
    prices = load_prices(_client(_catalog_handler))

    assert prices.unit_price("sql.vcpu_hour", "us-central1") == pytest.approx(0.0413)
    assert prices.unit_price("gcs.nearline.gb_month", "us-central1") == pytest.approx(0.01)
    assert prices.unit_price("run.vcpu_second_idle", "us-central1") == pytest.approx(2.5e-6)
    assert prices.unit_price("run.vcpu_second_idle", "europe-west3") == pytest.approx(3.5e-6)


def test_the_gke_fee_is_global_so_no_region_multiplier_applies():
    prices = load_prices(_client(_catalog_handler))

    for region in ("us-central1", "europe-west3", "asia-southeast1"):
        assert prices.unit_price("gke.cluster_fee_hour", region) == pytest.approx(0.10)


def test_a_sku_with_the_right_name_but_another_unit_is_not_a_price():
    early_delete = _sku("Nearline Storage Iowa", 333_330, ["us-central1"], unit="GiBy.d")

    assert prices_from_skus([early_delete]) == {}


def test_pagination_is_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        if "pageToken" in request.url.params:
            return httpx.Response(200, json={"skus": [SKUS[1]]})
        return httpx.Response(200, json={"skus": [SKUS[0]], "nextPageToken": "next"})

    assert len(_client(handler).list_skus("svc")) == 2


def test_transient_errors_are_retried_with_backoff():
    calls: list[int] = []
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"skus": SKUS})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = BillingCatalogClient(API_KEY, http=http, sleep=waits.append, backoff_s=0.5)

    assert len(client.list_skus("svc")) == len(SKUS)
    assert waits == [0.5, 1.0]


def test_falls_back_to_static_prices_when_the_api_keeps_failing():
    prices = load_prices(_client(lambda request: httpx.Response(500), max_retries=2))

    assert prices.source == "static"
    assert prices.may_be_outdated is True
    assert "HTTP 500" in prices.fallback_reason


def test_falls_back_on_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    prices = load_prices(_client(handler, max_retries=1))

    assert prices.source == "static"
    assert "ReadTimeout" in prices.fallback_reason


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>captive portal</html>"),
        httpx.Response(200, content=b"\xff\xfe not utf-8"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"skus": "not a list"}),
        httpx.Response(200, json={"skus": [], "nextPageToken": 7}),
        httpx.Response(200, json={"skus": [None, 1, {"category": None}, {"description": 5}]}),
        httpx.Response(200, json={"error": "something else entirely"}),
        httpx.Response(204),
    ],
    ids=["html", "bytes", "list", "skus", "token", "entries", "other", "empty"],
)
def test_an_answer_of_the_wrong_shape_means_static_prices(response):
    prices = load_prices(_client(lambda request: response, max_retries=1))

    assert prices.source == "static"
    assert prices.may_be_outdated is True
    assert prices.fallback_reason


def test_an_error_nobody_expected_still_means_static_prices(log_lines):
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"something broke with key={API_KEY}")

    prices = load_prices(_client(handler))

    assert prices.source == "static"
    assert "RuntimeError" in prices.fallback_reason
    assert API_KEY not in prices.fallback_reason
    causes = [line["causes"][0] for line in log_lines() if line["event"] == "billing.fallback"]
    assert len(causes) == len(SERVICES)
    assert "something broke" in causes[0]["message"]
    assert API_KEY not in str(causes)


def test_one_failing_service_does_not_take_the_others_down():
    broken = {Service.CLOUD_SQL: httpx.Response(200, text="not json")}

    prices = load_prices(_client(lambda r: _catalog_handler(r, broken), max_retries=0))

    assert prices.source == "billing_api"
    assert prices.unit_price("compute.e2.vcpu_hour", "us-central1") == pytest.approx(0.022)
    assert prices.unit_price("sql.vcpu_hour", "us-central1") == pytest.approx(0.0413)  # static
    assert prices.may_be_outdated is True
    assert "cloud_sql" in prices.fallback_reason
    assert "compute" not in prices.fallback_reason


def test_pagination_that_never_ends_is_stopped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"skus": [SKUS[0]], "nextPageToken": "again"})

    with pytest.raises(BillingApiError, match="pages"):
        _client(handler).list_skus("svc")


def test_a_service_that_keeps_failing_slowly_is_given_up_after_the_total_time_limit():
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        now[0] += 9.0  # every attempt is inside its own timeout
        return httpx.Response(503)

    client = _client(handler, max_retries=50, total_timeout_s=30.0, clock=lambda: now[0])

    with pytest.raises(BillingApiError, match="30 s"):
        client.list_skus("svc")
    assert now[0] == 36.0


def test_the_key_is_sent_in_a_header_and_never_in_the_url():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"skus": []})

    _client(handler).list_skus("svc")

    assert API_KEY not in str(seen[0].url)
    assert seen[0].headers["X-Goog-Api-Key"] == API_KEY


def test_no_client_means_static_prices():
    prices = load_prices(None)

    assert prices.source == "static"
    assert "GCP_BILLING_API_KEY" in prices.fallback_reason


def test_permission_errors_are_not_retried_and_never_leak_the_key():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, json={"error": {"message": f"key {API_KEY} is invalid"}})

    with pytest.raises(BillingApiError) as error:
        _client(handler).list_skus("svc")

    assert len(calls) == 1
    assert API_KEY not in str(error.value)
