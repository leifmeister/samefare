"""
Seed script — populates the database with realistic Icelandic ridesharing data.
Run from the project root:  python -m scripts.seed
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from passlib.context import CryptContext
from app.database import SessionLocal, Base, engine
from app import models

Base.metadata.create_all(bind=engine)
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
db  = SessionLocal()


def h(pw): return pwd.hash(pw)


# ── Users ──────────────────────────────────────────────────────────────────────
users = [
    models.User(email="gunnar@example.is",   full_name="Gunnar Sigurðsson",
                phone="+354 771 2233", hashed_password=h("password123"),
                bio="Driving the Ring Road for 15 years. Always on time."),
    models.User(email="sigrid@example.is",   full_name="Sigríður Magnúsdóttir",
                phone="+354 865 4411", hashed_password=h("password123"),
                bio="Based in Akureyri, heading south most weekends."),
    models.User(email="bjorn@example.is",    full_name="Björn Eiríksson",
                phone="+354 699 8877", hashed_password=h("password123"),
                bio="Electric car driver — quiet and smooth rides guaranteed."),
    models.User(email="anna@example.is",     full_name="Anna Helgadóttir",
                phone="+354 555 0123", hashed_password=h("password123"),
                bio="Regularly travel between Reykjavík and Selfoss for work."),
    models.User(email="magnus@example.is",   full_name="Magnús Þórsson",
                phone="+354 780 3344", hashed_password=h("password123"),
                bio="4x4 owner. Happy to take on highland routes in summer."),
    models.User(email="helga@example.is",    full_name="Helga Jónsdóttir",
                hashed_password=h("password123"),
                bio="Student at University of Iceland, commuting weekly."),
    models.User(email="passenger1@example.is", full_name="Ólafur Kristjánsson",
                hashed_password=h("password123")),
    models.User(email="passenger2@example.is", full_name="Ragnheiður Sveinsdóttir",
                hashed_password=h("password123")),
]
for u in users:
    db.add(u)
db.commit()
for u in users:
    db.refresh(u)

gunnar, sigrid, bjorn, anna, magnus, helga, pax1, pax2 = users

# ── Trips ──────────────────────────────────────────────────────────────────────
now = datetime.utcnow()

trips = [
    models.Trip(
        driver_id=gunnar.id,
        origin="Reykjavík", destination="Akureyri",
        departure_datetime=now + timedelta(days=2, hours=8),
        seats_total=3, seats_available=3,
        price_per_seat=4500,
        car_make="Toyota", car_model="Land Cruiser", car_year=2019,
        car_type="suv",
        description="Leaving from BSÍ bus terminal at 08:00. Happy to stop at Goðafoss.",
        allows_luggage=True, allows_pets=False, smoking=False,
    ),
    models.Trip(
        driver_id=sigrid.id,
        origin="Akureyri", destination="Reykjavík",
        departure_datetime=now + timedelta(days=3, hours=14),
        seats_total=2, seats_available=2,
        price_per_seat=4000,
        car_make="Kia", car_model="Sportage", car_year=2021,
        car_type="suv",
        description="Heading south after the weekend. Can stop in Borgarnes.",
        allows_luggage=True, allows_pets=True, smoking=False,
    ),
    models.Trip(
        driver_id=bjorn.id,
        origin="Reykjavík", destination="Selfoss",
        departure_datetime=now + timedelta(days=1, hours=7, minutes=30),
        seats_total=4, seats_available=4,
        price_per_seat=900,
        car_make="Tesla", car_model="Model 3", car_year=2022,
        car_type="electric",
        description="Silent electric ride. Supercharger stop in Selfoss.",
        allows_luggage=True, allows_pets=False, smoking=False,
    ),
    models.Trip(
        driver_id=anna.id,
        origin="Selfoss", destination="Reykjavík",
        departure_datetime=now + timedelta(days=1, hours=17),
        seats_total=3, seats_available=3,
        price_per_seat=850,
        car_make="Volkswagen", car_model="Golf", car_year=2020,
        car_type="sedan",
        description="Daily commute back to the city. Leaving Selfoss on the dot.",
        allows_luggage=False, allows_pets=False, smoking=False,
    ),
    models.Trip(
        driver_id=magnus.id,
        origin="Reykjavík", destination="Vík",
        departure_datetime=now + timedelta(days=4, hours=9),
        seats_total=3, seats_available=3,
        price_per_seat=2800,
        car_make="Toyota", car_model="Hilux", car_year=2018,
        car_type="4x4",
        description="South coast drive. Stopping at Skógafoss and black sand beach.",
        allows_luggage=True, allows_pets=True, smoking=False,
    ),
    models.Trip(
        driver_id=gunnar.id,
        origin="Reykjavík", destination="Keflavík",
        departure_datetime=now + timedelta(days=1, hours=4, minutes=30),
        seats_total=4, seats_available=4,
        price_per_seat=1100,
        car_make="Toyota", car_model="Land Cruiser", car_year=2019,
        car_type="suv",
        description="Airport drop-off. Plenty of space for luggage. Early morning departure.",
        allows_luggage=True, allows_pets=False, smoking=False,
    ),
    models.Trip(
        driver_id=helga.id,
        origin="Reykjavík", destination="Borgarnes",
        departure_datetime=now + timedelta(days=5, hours=16),
        seats_total=2, seats_available=2,
        price_per_seat=1400,
        car_make="Honda", car_model="Civic", car_year=2017,
        car_type="sedan",
        description="Heading home for the weekend. Can pick up near Hlemmur.",
        allows_luggage=True, allows_pets=False, smoking=False,
    ),
    models.Trip(
        driver_id=sigrid.id,
        origin="Akureyri", destination="Húsavík",
        departure_datetime=now + timedelta(days=6, hours=10),
        seats_total=3, seats_available=3,
        price_per_seat=1600,
        car_make="Kia", car_model="Sportage", car_year=2021,
        car_type="suv",
        description="Whale watching day trip from Húsavík. Return same day or stay overnight.",
        allows_luggage=True, allows_pets=True, smoking=False,
    ),
    models.Trip(
        driver_id=bjorn.id,
        origin="Reykjavík", destination="Höfn",
        departure_datetime=now + timedelta(days=7, hours=7),
        seats_total=2, seats_available=2,
        price_per_seat=5500,
        car_make="Tesla", car_model="Model 3", car_year=2022,
        car_type="electric",
        description="Full day drive to Höfn via the glacier lagoon. One charging stop.",
        allows_luggage=True, allows_pets=False, smoking=False,
    ),
    models.Trip(
        driver_id=anna.id,
        origin="Reykjavík", destination="Hveragerði",
        departure_datetime=now + timedelta(days=2, hours=8, minutes=15),
        seats_total=3, seats_available=3,
        price_per_seat=700,
        car_make="Volkswagen", car_model="Golf", car_year=2020,
        car_type="sedan",
        description="Quick run to Hveragerði. Can drop at the geothermal pool.",
        allows_luggage=False, allows_pets=False, smoking=False,
    ),
]
for t in trips:
    db.add(t)
db.commit()
for t in trips:
    db.refresh(t)

# ── Sample bookings (confirmed) ────────────────────────────────────────────────
booking1 = models.Booking(
    trip_id=trips[0].id, passenger_id=pax1.id,
    seats_booked=1, total_price=4860, service_fee=360,
    message="I'll be at BSÍ at 07:50. Thank you!",
    status=models.BookingStatus.confirmed,
)
booking2 = models.Booking(
    trip_id=trips[2].id, passenger_id=pax2.id,
    seats_booked=2, total_price=1944, service_fee=144,
    message="Two of us, no big bags.",
    status=models.BookingStatus.confirmed,
)
# Pending booking
booking3 = models.Booking(
    trip_id=trips[4].id, passenger_id=pax1.id,
    seats_booked=1, total_price=3024, service_fee=224,
    message="Can you stop briefly at Seljalandsfoss?",
    status=models.BookingStatus.pending,
)
for b in [booking1, booking2, booking3]:
    db.add(b)

# Adjust seats_available for confirmed bookings
trips[0].seats_available -= 1
trips[2].seats_available -= 2
trips[4].seats_available -= 1
db.commit()

# ── Reviews ────────────────────────────────────────────────────────────────────
db.refresh(booking1); db.refresh(booking2)

review1 = models.Review(
    booking_id=booking1.id, trip_id=trips[0].id,
    reviewer_id=pax1.id, reviewee_id=gunnar.id,
    review_type=models.ReviewType.passenger_to_driver,
    rating=5, comment="Fantastic driver, punctual, great conversation. Highly recommend!",
)
review2 = models.Review(
    booking_id=booking1.id, trip_id=trips[0].id,
    reviewer_id=gunnar.id, reviewee_id=pax1.id,
    review_type=models.ReviewType.driver_to_passenger,
    rating=5, comment="Great passenger, on time and easy going.",
)
review3 = models.Review(
    booking_id=booking2.id, trip_id=trips[2].id,
    reviewer_id=pax2.id, reviewee_id=bjorn.id,
    review_type=models.ReviewType.passenger_to_driver,
    rating=5, comment="Electric car ride was so smooth and quiet. Bjorn is a superb driver.",
)
for r in [review1, review2, review3]:
    db.add(r)
db.commit()

print("✓ Database seeded successfully!")
print(f"  {len(users)} users, {len(trips)} trips, 3 bookings, 3 reviews")
print("\nTest accounts (password: password123):")
for u in users[:6]:
    print(f"  {u.email}")

db.close()
