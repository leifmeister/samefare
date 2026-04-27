"""
Seed test users and rides for development / demo purposes.

Run once from the project root:
    python seed_test_data.py

Safe to re-run — skips any email that already exists.
Credentials are shown in the admin panel at /admin/test-users.
"""

import os, sys
from datetime import datetime, timedelta

# Make sure app/ is importable
sys.path.insert(0, os.path.dirname(__file__))

import bcrypt as _bcrypt
from app.database import SessionLocal
from app import models


def _hash(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()

# ── Test account definitions ──────────────────────────────────────────────────
# These are also read by the admin panel — keep in sync with TEST_USERS in
# app/routers/verification.py if you ever change emails or passwords.

TEST_USERS = [
    {
        "full_name":   "Sigríður Björnsdóttir",
        "email":       "sigga@test.samefare.com",
        "password":    "Test1234!",
        "phone":       "+354 7751234",
        "bio":         "Driving the Ring Road every other weekend. Happy to share fuel costs and good conversation.",
        "id_verification":      "approved",
        "license_verification": "approved",
        "id_doc_type":          "license",
        "email_verified":       True,
        "trips": [
            {
                "origin": "Reykjavík", "destination": "Akureyri",
                "days_ahead": 3, "hour": 8,
                "seats_total": 3, "price_per_seat": 4500,
                "car_make": "Toyota", "car_model": "RAV4", "car_year": 2021,
                "car_type": "suv",
                "description": "Leaving from BSÍ bus terminal. Stopping in Borgarnes for coffee.",
                "allows_luggage": True, "allows_pets": False, "smoking": False,
                "instant_book": True,
            },
            {
                "origin": "Akureyri", "destination": "Reykjavík",
                "days_ahead": 5, "hour": 14,
                "seats_total": 3, "price_per_seat": 4500,
                "car_make": "Toyota", "car_model": "RAV4", "car_year": 2021,
                "car_type": "suv",
                "description": "Return leg. Same comfort, same stops.",
                "allows_luggage": True, "allows_pets": False, "smoking": False,
                "instant_book": True,
            },
            {
                "origin": "Reykjavík", "destination": "Vík",
                "days_ahead": 8, "hour": 9,
                "seats_total": 2, "price_per_seat": 3200,
                "car_make": "Toyota", "car_model": "RAV4", "car_year": 2021,
                "car_type": "suv",
                "description": "Day trip to the south coast. Back same evening — no return seats.",
                "allows_luggage": False, "allows_pets": False, "smoking": False,
                "instant_book": False,
            },
        ],
    },
    {
        "full_name":   "Ólafur Gunnarsson",
        "email":       "oli@test.samefare.com",
        "password":    "Test1234!",
        "phone":       "+354 6989876",
        "bio":         "Student at University of Iceland. Commuting to Selfoss every Friday.",
        "id_verification":      "approved",
        "license_verification": "approved",
        "id_doc_type":          "license",
        "email_verified":       True,
        "trips": [
            {
                "origin": "Reykjavík", "destination": "Selfoss",
                "days_ahead": 2, "hour": 17,
                "seats_total": 2, "price_per_seat": 1800,
                "car_make": "Volkswagen", "car_model": "Golf", "car_year": 2019,
                "car_type": "sedan",
                "description": "Heading home for the weekend every Friday. Reliable and on time.",
                "allows_luggage": True, "allows_pets": True, "smoking": False,
                "instant_book": True,
            },
            {
                "origin": "Selfoss", "destination": "Reykjavík",
                "days_ahead": 4, "hour": 7,
                "seats_total": 2, "price_per_seat": 1800,
                "car_make": "Volkswagen", "car_model": "Golf", "car_year": 2019,
                "car_type": "sedan",
                "description": "Monday morning return. Early start — 07:00 sharp.",
                "allows_luggage": True, "allows_pets": True, "smoking": False,
                "instant_book": True,
            },
        ],
    },
    {
        "full_name":   "Katrín Magnúsdóttir",
        "email":       "katrin@test.samefare.com",
        "password":    "Test1234!",
        "phone":       "+354 8223344",
        "bio":         "Nature photographer. Regularly driving to the highlands and Westfjords.",
        "id_verification":      "approved",
        "license_verification": "approved",
        "id_doc_type":          "license",
        "email_verified":       True,
        "trips": [
            {
                "origin": "Reykjavík", "destination": "Ísafjörður",
                "days_ahead": 6, "hour": 6,
                "seats_total": 4, "price_per_seat": 6500,
                "car_make": "Mitsubishi", "car_model": "Outlander", "car_year": 2022,
                "car_type": "4x4",
                "description": "Westfjords photography trip. 4WD — comfortable on F-roads. Luggage space limited to daypacks.",
                "allows_luggage": False, "allows_pets": False, "smoking": False,
                "instant_book": False,
            },
            {
                "origin": "Reykjavík", "destination": "Keflavík",
                "days_ahead": 1, "hour": 5,
                "seats_total": 3, "price_per_seat": 1500,
                "car_make": "Mitsubishi", "car_model": "Outlander", "car_year": 2022,
                "car_type": "4x4",
                "description": "Early airport run. Terminal drop-off.",
                "allows_luggage": True, "allows_pets": False, "smoking": False,
                "instant_book": True,
            },
        ],
    },
    {
        "full_name":   "Björn Einarsson",
        "email":       "bjorn@test.samefare.com",
        "password":    "Test1234!",
        "phone":       "",
        "bio":         "",
        "id_verification":      "unverified",
        "license_verification": "unverified",
        "id_doc_type":          None,
        "email_verified":       True,
        "trips": [],   # passenger-only account, no rides posted
    },
]

# ── Seed ──────────────────────────────────────────────────────────────────────

def run():
    db = SessionLocal()
    now = datetime.utcnow()
    created = 0

    try:
        for u in TEST_USERS:
            if db.query(models.User).filter(models.User.email == u["email"]).first():
                print(f"  skip  {u['email']}  (already exists)")
                continue

            user = models.User(
                full_name            = u["full_name"],
                email                = u["email"],
                hashed_password      = _hash(u["password"]),
                phone                = u["phone"] or None,
                bio                  = u["bio"] or None,
                email_verified       = u["email_verified"],
                id_verification      = u["id_verification"],
                license_verification = u["license_verification"],
                id_doc_type          = u["id_doc_type"],
            )
            db.add(user)
            db.flush()  # get user.id before adding trips

            for t in u["trips"]:
                departure = now.replace(
                    hour=t["hour"], minute=0, second=0, microsecond=0
                ) + timedelta(days=t["days_ahead"])
                trip = models.Trip(
                    driver_id      = user.id,
                    origin         = t["origin"],
                    destination    = t["destination"],
                    departure_datetime = departure,
                    seats_total    = t["seats_total"],
                    seats_available= t["seats_total"],
                    price_per_seat = t["price_per_seat"],
                    car_make       = t["car_make"],
                    car_model      = t["car_model"],
                    car_year       = t["car_year"],
                    car_type       = t["car_type"],
                    description    = t["description"],
                    allows_luggage = t["allows_luggage"],
                    allows_pets    = t["allows_pets"],
                    smoking        = t["smoking"],
                    instant_book   = t["instant_book"],
                    status         = models.TripStatus.active,
                )
                db.add(trip)

            db.commit()
            print(f"  added {u['email']}  ({len(u['trips'])} trip{'s' if len(u['trips']) != 1 else ''})")
            created += 1

    finally:
        db.close()

    print(f"\nDone — {created} new account(s) created.")
    if created > 0:
        print("Credentials are visible at /admin/test-users when logged in as admin.")


if __name__ == "__main__":
    run()
