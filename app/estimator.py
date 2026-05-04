"""
Trip cost estimator — pure, deterministic, no database side effects.

Business rules
--------------
1. Total passenger payments must not exceed the driver's estimated trip cost
   multiplied by the passenger share (seats / seats+1).
   The driver's own seat is never charged to passengers.

2. allowed_cost_per_km = min(raw_cost_per_km, platform_cap)
   This prevents extreme vehicle classes from producing unreasonable caps.

3. price_per_seat_cap is always rounded DOWN to the nearest rounding_unit
   (default 50 ISK).  It is never rounded up.

4. Drivers set a price at or below price_per_seat_cap.
   They cannot set a price that exceeds the cap.

Cost formula (per km)
---------------------
    fuel_or_energy_cost_per_km          (petrol/diesel: L/100km × ISK/L / 100)
                                        (electric:      kWh/100km × ISK/kWh / 100)
  + kilometragjald_per_km               (statutory road-use charge, all vehicles)
  + wear_and_tear_isk_per_km            (tyres, brakes, filters, oil)
  + real_depreciation * factor          (marginal usage fraction only)
  ────────────────────────────────────
  = raw_cost_per_km
  → allowed_cost_per_km = min(raw, platform_cap)

Per-seat cap
------------
    total_trip_cost   = distance_km × allowed_cost_per_km
    price_per_seat_raw = total_trip_cost / (seats_total + 1)
                        # = driver's 1/(n+1) share per seat = passenger's fair share
    price_per_seat_cap = floor(price_per_seat_raw / rounding_unit) × rounding_unit

Transparency
------------
estimate_trip_cost() returns a TripCostEstimate dataclass that contains every
input and every intermediate value.  This object should be JSON-serialised and
stored on Trip.price_snapshot at trip creation so any historical trip's cap can
be reproduced exactly — important for regulatory defence.

The public methodology page (/pricing/how-it-works) explains this formula in
plain Icelandic and English.
"""

import dataclasses
import json
import math
import logging
from datetime import datetime

from types import SimpleNamespace
from app import models
from app.fuel import active_policy
from app.utils import build_route_graph, shortest_path_km

log = logging.getLogger(__name__)


# ── Vehicle class / fuel type mapping ─────────────────────────────────────────

# Maps CarType string values to the internal vehicle class used to look up
# default consumption values from the pricing policy.
_VEHICLE_CLASS: dict[str, str] = {
    "sedan":    "standard",
    "suv":      "suv",
    "van":      "van",
    "electric": "standard",   # body type standard; fuel type electric
    "4x4":      "suv",
    "camper":   "van",
}

# Default fuel type inferred from car_type when trip.fuel_type is NULL.
# The 'electric' CarType value predates the explicit FuelType column;
# everything else defaults to petrol.
_INFERRED_FUEL_TYPE: dict[str, str] = {
    "sedan":    "petrol",
    "suv":      "petrol",
    "van":      "petrol",
    "electric": "electric",
    "4x4":      "petrol",
    "camper":   "petrol",
}


def _vehicle_class(car_type: str | None) -> str:
    return _VEHICLE_CLASS.get(str(car_type) if car_type else "sedan", "standard")


def _infer_fuel_type(car_type: str | None, fuel_type: str | None) -> str:
    if fuel_type:
        return str(fuel_type)
    return _INFERRED_FUEL_TYPE.get(str(car_type) if car_type else "sedan", "petrol")


# ── Output dataclass ──────────────────────────────────────────────────────────

@dataclasses.dataclass
class TripCostEstimate:
    """
    Complete cost breakdown for a single trip.

    All ISK/km values are floats; all ISK totals are floats until the final
    price_per_seat_cap which is an int (rounded down).

    This object is JSON-serialisable via dataclasses.asdict() + json.dumps().
    Store it on Trip.price_snapshot for the audit trail.
    """
    # ── Inputs ────────────────────────────────────────────────────────────────
    distance_km:               float
    seats_total:               int      # seats offered (excludes driver)
    fuel_type:                 str      # 'petrol', 'diesel', 'electric', 'hybrid'
    vehicle_class:             str      # 'small', 'standard', 'suv', 'van'
    fuel_price_isk_per_liter:  float    # p80 or fallback; used for petrol/diesel
    fuel_price_tier:           str      # 'live', 'cached', 'fallback'
    electricity_price_isk_per_kwh: float

    # ── Per-km cost breakdown ─────────────────────────────────────────────────
    fuel_or_energy_cost_per_km:  float
    kilometragjald_per_km:       float
    wear_and_tear_per_km:        float
    partial_depreciation_per_km: float  # = real_depreciation × depreciation_factor

    # ── Aggregates ────────────────────────────────────────────────────────────
    raw_cost_per_km:     float    # sum of all per-km components
    allowed_cost_per_km: float    # min(raw, platform_cap)
    was_capped:          bool     # True if the platform cap was the binding constraint

    # ── Trip-level ────────────────────────────────────────────────────────────
    total_trip_cost:      float   # distance × allowed_cost_per_km (unrounded)
    price_per_seat_raw:   float   # total / (seats + 1), before rounding
    price_per_seat_cap:   int     # floor(raw / rounding_unit) × rounding_unit

    # ── Policy metadata (for snapshot reproducibility) ────────────────────────
    policy_id:             int
    policy_effective_from: str    # ISO date string
    rounding_unit:         int
    platform_cap:          float
    calculation_timestamp: str    # ISO datetime, UTC

    def to_json(self) -> str:
        """Serialise to JSON string for storage in Trip.price_snapshot."""
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "TripCostEstimate":
        """Deserialise from Trip.price_snapshot."""
        return cls(**json.loads(raw))


# ── Core estimator ────────────────────────────────────────────────────────────

def estimate_trip_cost(
    distance_km:  float,
    seats_total:  int,
    car_type:     str | None,
    fuel_type:    str | None,
    fuel_price:   float,
    fuel_price_tier: str,
    policy:       models.PricingPolicy,
) -> TripCostEstimate:
    """
    Compute a full cost estimate for a trip.  Pure function — no DB access.

    Parameters
    ----------
    distance_km
        Route distance in kilometres.
    seats_total
        Number of paid passenger seats offered (1–8, driver not counted).
    car_type
        Trip.car_type string value ('sedan', 'suv', etc.).
    fuel_type
        Trip.fuel_type string value, or None to infer from car_type.
    fuel_price
        Current p80 petrol price in ISK/L (from get_current_petrol_price()).
    fuel_price_tier
        'live', 'cached', or 'fallback' — stored in snapshot for transparency.
    policy
        Active PricingPolicy row.
    """
    v_class   = _vehicle_class(car_type)
    f_type    = _infer_fuel_type(car_type, fuel_type)
    elec_price = float(policy.electricity_price_isk_per_kwh)

    # ── Fuel / energy cost per km ──────────────────────────────────────────────
    if f_type == "electric":
        consumption = (
            float(policy.ev_consumption_suv)
            if v_class == "suv"
            else float(policy.ev_consumption_standard)
        )
        fuel_or_energy = consumption * elec_price / 100.0
    else:
        # petrol, diesel, hybrid — all use the petrol price as proxy
        # (diesel is slightly cheaper but we keep one price source for simplicity)
        if v_class == "small":
            consumption = float(policy.consumption_small)
        elif v_class == "suv":
            consumption = float(policy.consumption_suv)
        elif v_class == "van":
            consumption = float(policy.consumption_van)
        else:
            consumption = float(policy.consumption_standard)
        fuel_or_energy = consumption * fuel_price / 100.0

    # ── Other per-km components ────────────────────────────────────────────────
    kilometragjald       = float(policy.kilometragjald_standard)
    wear_and_tear        = float(policy.wear_and_tear_isk_per_km)
    partial_depreciation = (
        float(policy.real_depreciation_isk_per_km)
        * float(policy.depreciation_factor)
    )

    raw_cost  = fuel_or_energy + kilometragjald + wear_and_tear + partial_depreciation
    cap       = float(policy.platform_cost_cap_isk_per_km)
    allowed   = min(raw_cost, cap)
    was_capped = allowed < raw_cost

    # ── Trip-level totals ──────────────────────────────────────────────────────
    total_trip_cost    = distance_km * allowed
    # Driver covers 1/(seats+1); each passenger covers the same share.
    price_per_seat_raw = total_trip_cost / (seats_total + 1)
    rounding           = int(policy.rounding_unit)
    price_per_seat_cap = math.floor(price_per_seat_raw / rounding) * rounding

    return TripCostEstimate(
        distance_km              = distance_km,
        seats_total              = seats_total,
        fuel_type                = f_type,
        vehicle_class            = v_class,
        fuel_price_isk_per_liter = fuel_price,
        fuel_price_tier          = fuel_price_tier,
        electricity_price_isk_per_kwh = elec_price,

        fuel_or_energy_cost_per_km  = round(fuel_or_energy,        4),
        kilometragjald_per_km       = round(kilometragjald,         4),
        wear_and_tear_per_km        = round(wear_and_tear,          4),
        partial_depreciation_per_km = round(partial_depreciation,   4),

        raw_cost_per_km     = round(raw_cost, 4),
        allowed_cost_per_km = round(allowed,  4),
        was_capped          = was_capped,

        total_trip_cost    = round(total_trip_cost,    2),
        price_per_seat_raw = round(price_per_seat_raw, 2),
        price_per_seat_cap = price_per_seat_cap,

        policy_id             = policy.id,
        policy_effective_from = str(policy.effective_from),
        rounding_unit         = rounding,
        platform_cap          = cap,
        calculation_timestamp = datetime.utcnow().isoformat(),
    )


def estimate_for_trip(
    trip: models.Trip,
    db,
    fuel_price: float | None = None,
    fuel_price_tier: str | None = None,
) -> TripCostEstimate | None:
    """
    High-level helper: look up the active policy and route distance, then
    call estimate_trip_cost().

    Returns None if:
      - No active pricing policy exists
      - No route distance can be found (route not in the route table)

    If fuel_price is provided (e.g. pre-fetched for a batch), it is used
    directly; otherwise get_current_petrol_price() is called.
    """
    from app.fuel import get_current_petrol_price

    policy = active_policy(db)
    if policy is None:
        log.warning("estimate_for_trip: no active pricing policy")
        return None

    route = (
        db.query(models.Route)
        .filter(
            models.Route.origin      == trip.origin,
            models.Route.destination == trip.destination,
            models.Route.is_active   == True,   # noqa: E712
        )
        .first()
    )
    if route is None:
        log.debug(
            "estimate_for_trip: no route found for %s → %s",
            trip.origin, trip.destination,
        )
        return None

    if fuel_price is None:
        fuel_price, fuel_price_tier = get_current_petrol_price(db)

    return estimate_trip_cost(
        distance_km      = float(route.distance_km),
        seats_total      = trip.seats_total,
        car_type         = str(trip.car_type) if trip.car_type else None,
        fuel_type        = str(trip.fuel_type) if trip.fuel_type else None,
        fuel_price       = fuel_price,
        fuel_price_tier  = fuel_price_tier or "fallback",
        policy           = policy,
    )


def route_lookup(origin: str, destination: str, db):
    """
    Return the active Route for a city pair, or a synthetic object with
    distance_km computed via Dijkstra for multi-hop routes not stored as
    direct rows.  Returns None only when no path exists at all.
    """
    direct = (
        db.query(models.Route)
        .filter(
            models.Route.origin      == origin,
            models.Route.destination == destination,
            models.Route.is_active   == True,   # noqa: E712
        )
        .first()
    )
    if direct:
        return direct
    km = shortest_path_km(build_route_graph(db), origin, destination)
    return SimpleNamespace(distance_km=km) if km else None
