"""
Fuel price fetcher — apis.is /petrol endpoint.

Three-tier fallback, in order:
  1. Live fetch from apis.is  → p80 of all stations, validated, stored in cache
  2. Stale DB cache           → most recent row within MAX_CACHE_AGE_DAYS
  3. Policy table fallback    → hardcoded conservative estimate

p80 means 80 % of Icelandic petrol stations currently charge at or below
this price.  It is generous toward drivers without being skewed by outlier-
expensive remote stations, and it can be stated plainly on the methodology
page: "fuel price is the 80th percentile of current national station prices,
refreshed daily."

The fetch is synchronous (urllib.request) to match the house style.
Callers that need the current price should call get_current_petrol_price()
and pass the returned tier string to the estimator for transparency.
"""

import json
import logging
import statistics
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from app import models

log = logging.getLogger(__name__)

_APIS_IS_URL      = "https://apis.is/petrol"
_REQUEST_TIMEOUT  = 10        # seconds
MAX_CACHE_AGE_DAYS = 7        # use cached price if fetch fails, up to this many days old
MIN_STATION_COUNT  = 10       # reject fetch if fewer stations returned than this


# ── p80 helper ────────────────────────────────────────────────────────────────

def _percentile_80(prices: list[float]) -> float:
    """
    Return the 80th percentile of a list of prices.

    Uses nearest-rank method: the value at index floor(0.80 * n) when sorted.
    Always returns a value from the list — no interpolation.
    """
    sorted_prices = sorted(prices)
    idx = min(int(len(sorted_prices) * 0.80), len(sorted_prices) - 1)
    return sorted_prices[idx]


# ── Live fetch ────────────────────────────────────────────────────────────────

def _fetch_live(policy: models.PricingPolicy) -> float | None:
    """
    Fetch station prices from apis.is and return the p80.

    Returns None (and logs a warning) if:
      - The HTTP request fails
      - Fewer than MIN_STATION_COUNT valid prices are returned
      - The computed p80 falls outside the policy's sanity bounds
    """
    try:
        req  = urllib.request.Request(_APIS_IS_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        log.warning("apis.is fetch failed (network): %s", exc)
        return None
    except Exception as exc:
        log.warning("apis.is fetch failed (parse): %s", exc)
        return None

    results = data.get("results", [])
    prices: list[float] = []
    for station in results:
        raw = station.get("bensin95")
        if raw and isinstance(raw, (int, float)) and raw > 0:
            prices.append(float(raw))

    if len(prices) < MIN_STATION_COUNT:
        log.warning(
            "apis.is returned only %d valid petrol prices (need >= %d) — rejecting",
            len(prices), MIN_STATION_COUNT,
        )
        return None

    p80    = _percentile_80(prices)
    median = statistics.median(prices)

    lo = float(policy.fuel_price_min_isk_per_liter)
    hi = float(policy.fuel_price_max_isk_per_liter)
    if not (lo <= p80 <= hi):
        log.warning(
            "apis.is p80=%.1f ISK/L is outside sanity bounds [%.0f, %.0f] — "
            "possible data glitch, falling back to cache",
            p80, lo, hi,
        )
        return None

    log.info(
        "Fuel price refreshed from apis.is: p80=%.1f ISK/L  "
        "median=%.1f ISK/L  stations=%d",
        p80, median, len(prices),
    )
    return p80


def _store_cache(db, p80: float, median: float | None, count: int | None) -> None:
    """Append a new row to fuel_price_cache.  Errors are logged, not raised."""
    try:
        entry = models.FuelPriceCache(
            fuel_type     = "petrol",
            p80_price     = p80,
            median_price  = median,
            station_count = count,
            source        = "apis_is",
            fetched_at    = datetime.utcnow(),
        )
        db.add(entry)
        db.commit()
    except Exception as exc:
        log.warning("Failed to persist fuel price cache row: %s", exc)
        db.rollback()


# ── Active policy helper (shared with estimator) ──────────────────────────────

def active_policy(db) -> models.PricingPolicy | None:
    """
    Return the PricingPolicy row that is active today, or None if not found.

    Picks the most recently effective row where
    effective_from <= today AND (effective_to IS NULL OR effective_to >= today).
    """
    today = date.today()
    return (
        db.query(models.PricingPolicy)
        .filter(
            models.PricingPolicy.effective_from <= today,
            (models.PricingPolicy.effective_to == None)       # noqa: E711
            | (models.PricingPolicy.effective_to >= today),
        )
        .order_by(models.PricingPolicy.effective_from.desc())
        .first()
    )


# ── Public API ────────────────────────────────────────────────────────────────

def get_cached_petrol_price(db) -> tuple[float, str]:
    """
    Return ``(price_isk_per_liter, tier)`` using **only** the DB cache and the
    policy fallback.  Never makes an HTTP request — safe to call on every
    user-facing page load.

    The background task (``refresh_fuel_price``) keeps the cache warm; this
    function just reads what is already there.
    """
    policy = active_policy(db)
    if policy is None:
        return 290.0, "fallback"

    cutoff = datetime.utcnow() - timedelta(days=MAX_CACHE_AGE_DAYS)
    cached = (
        db.query(models.FuelPriceCache)
        .filter(
            models.FuelPriceCache.fuel_type  == "petrol",
            models.FuelPriceCache.fetched_at >= cutoff,
        )
        .order_by(models.FuelPriceCache.fetched_at.desc())
        .first()
    )
    if cached:
        return float(cached.p80_price), "cached"

    return float(policy.fuel_price_fallback_isk_per_liter), "fallback"


def get_current_petrol_price(db) -> tuple[float, str]:
    """
    Return ``(price_isk_per_liter, tier)`` where *tier* is one of:

    ``'live'``
        Freshly fetched from apis.is and stored in the cache.
    ``'cached'``
        Live fetch failed; using the most recent DB cache row
        (at most MAX_CACHE_AGE_DAYS old).
    ``'fallback'``
        Both live fetch and cache failed or are too stale; using the
        policy table's conservative fallback value.

    Never raises — always returns a usable price.
    """
    policy = active_policy(db)
    if policy is None:
        log.error(
            "No active pricing policy found — using hardcoded emergency fallback 290 ISK/L. "
            "Seed the pricing_policy table."
        )
        return 290.0, "fallback"

    # ── Tier 1: live fetch ────────────────────────────────────────────────────
    p80 = _fetch_live(policy)
    if p80 is not None:
        # Fire-and-forget: store in cache for future fallback use.
        # We do a quick second pass to get median/count for the cache row.
        try:
            req = urllib.request.Request(_APIS_IS_URL, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
            prices = [
                float(s["bensin95"])
                for s in data.get("results", [])
                if s.get("bensin95") and isinstance(s["bensin95"], (int, float))
            ]
            median = statistics.median(prices) if prices else None
            _store_cache(db, p80, median, len(prices))
        except Exception:
            _store_cache(db, p80, None, None)
        return p80, "live"

    # ── Tier 2: stale cache ───────────────────────────────────────────────────
    cutoff = datetime.utcnow() - timedelta(days=MAX_CACHE_AGE_DAYS)
    cached = (
        db.query(models.FuelPriceCache)
        .filter(
            models.FuelPriceCache.fuel_type  == "petrol",
            models.FuelPriceCache.fetched_at >= cutoff,
        )
        .order_by(models.FuelPriceCache.fetched_at.desc())
        .first()
    )
    if cached:
        age_days = (datetime.utcnow() - cached.fetched_at).days
        log.warning(
            "Live fuel fetch failed — using %d-day-old cache: %.1f ISK/L",
            age_days, float(cached.p80_price),
        )
        return float(cached.p80_price), "cached"

    # ── Tier 3: policy fallback ───────────────────────────────────────────────
    fallback = float(policy.fuel_price_fallback_isk_per_liter)
    log.error(
        "Fuel price cache is empty or older than %d days — "
        "using policy fallback %.1f ISK/L.  "
        "Check apis.is connectivity and the fuel_price_cache table.",
        MAX_CACHE_AGE_DAYS, fallback,
    )
    return fallback, "fallback"


def refresh_fuel_price(db) -> None:
    """
    Convenience wrapper for the background task: fetch and cache the current
    petrol price, logging the tier used.  Does not return the price — callers
    that need the price should call get_current_petrol_price().
    """
    price, tier = get_current_petrol_price(db)
    if tier == "fallback":
        log.error("Fuel price refresh: using policy fallback %.1f ISK/L", price)
    elif tier == "cached":
        log.warning("Fuel price refresh: using cached price %.1f ISK/L", price)
    else:
        log.info("Fuel price refresh: live price %.1f ISK/L", price)
