from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, Enum,
    ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


# ── Enumerations ──────────────────────────────────────────────────────────────
# Python 3.11+ changed str(StrEnum.member) to return "ClassName.member".
# The __str__ override restores the previous behaviour (returns the value)
# so templates, CSS classes, and DB comparisons all work without .value calls.

class _StrEnum(str, PyEnum):
    def __str__(self) -> str:
        return self.value


class UserRole(_StrEnum):
    driver    = "driver"
    passenger = "passenger"
    both      = "both"


class CarType(_StrEnum):
    sedan        = "sedan"
    suv          = "suv"
    van          = "van"
    electric     = "electric"
    four_by_four = "4x4"
    camper       = "camper"


class TripStatus(_StrEnum):
    active    = "active"
    completed = "completed"
    cancelled = "cancelled"


class BookingStatus(_StrEnum):
    awaiting_payment = "awaiting_payment"
    pending          = "pending"
    confirmed        = "confirmed"
    rejected         = "rejected"
    cancelled        = "cancelled"
    completed        = "completed"


class PaymentStatus(_StrEnum):
    authorised     = "authorised"
    captured       = "captured"
    refunded       = "refunded"
    partial_refund = "partial_refund"


class ReviewType(_StrEnum):
    passenger_to_driver = "passenger_to_driver"
    driver_to_passenger = "driver_to_passenger"


class VerificationStatus(_StrEnum):
    unverified = "unverified"
    pending    = "pending"
    approved   = "approved"
    rejected   = "rejected"


# ── Users ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True)
    email           = Column(String(255), unique=True, nullable=False)
    full_name       = Column(String(255), nullable=False)
    phone           = Column(String(50))
    hashed_password = Column(String(255), nullable=False)
    role            = Column(Enum(UserRole), nullable=False, default=UserRole.both)
    is_active       = Column(Boolean, nullable=False, default=True)
    is_admin        = Column(Boolean, nullable=False, default=False)
    avatar_url      = Column(String(512))
    bio             = Column(Text)
    created_at      = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at      = Column(DateTime, nullable=False, default=datetime.utcnow,
                             onupdate=datetime.utcnow)

    # Phone verification
    phone_verified      = Column(Boolean, nullable=False, default=False)
    phone_otp           = Column(String(6))
    phone_otp_expires   = Column(DateTime)

    # Password reset
    reset_token         = Column(String(64))
    reset_token_expires = Column(DateTime)

    # Identity & licence verification
    id_verification          = Column(Enum(VerificationStatus), nullable=False,
                                      default=VerificationStatus.unverified)
    license_verification     = Column(Enum(VerificationStatus), nullable=False,
                                      default=VerificationStatus.unverified)
    id_doc_filename          = Column(String(255))
    license_doc_filename     = Column(String(255))
    id_rejection_reason      = Column(Text)
    license_rejection_reason = Column(Text)

    trips           = relationship("Trip",    back_populates="driver",
                                   cascade="all, delete-orphan")
    bookings        = relationship("Booking", back_populates="passenger",
                                   foreign_keys="Booking.passenger_id")
    reviews_given   = relationship("Review",  back_populates="reviewer",
                                   foreign_keys="Review.reviewer_id")
    reviews_received = relationship("Review", back_populates="reviewee",
                                    foreign_keys="Review.reviewee_id")
    messages_sent   = relationship("Message", back_populates="sender",
                                   foreign_keys="Message.sender_id")

    __table_args__ = (Index("ix_users_email", "email"),)

    @property
    def average_rating(self) -> float | None:
        if not self.reviews_received:
            return None
        return round(sum(r.rating for r in self.reviews_received) / len(self.reviews_received), 1)

    @property
    def total_trips_as_driver(self) -> int:
        return sum(1 for t in self.trips if t.status == TripStatus.completed)

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"


# ── Trips ─────────────────────────────────────────────────────────────────────

class Trip(Base):
    __tablename__ = "trips"

    id                 = Column(Integer, primary_key=True)
    driver_id          = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                                nullable=False)
    origin             = Column(String(150), nullable=False)
    destination        = Column(String(150), nullable=False)
    departure_datetime = Column(DateTime,    nullable=False)
    seats_total        = Column(SmallInteger, nullable=False, default=3)
    seats_available    = Column(SmallInteger, nullable=False, default=3)
    # Price in ISK (integer to avoid float drift)
    price_per_seat     = Column(Integer, nullable=False)
    car_make           = Column(String(100))
    car_model          = Column(String(100))
    car_year           = Column(Integer)
    car_type           = Column(Enum(CarType), nullable=False, default=CarType.sedan)
    description        = Column(Text)
    pickup_address     = Column(String(255))   # exact pickup spot within origin city
    dropoff_address    = Column(String(255))   # exact dropoff spot within destination city
    allows_luggage     = Column(Boolean, nullable=False, default=True)
    allows_pets        = Column(Boolean, nullable=False, default=False)
    smoking            = Column(Boolean, nullable=False, default=False)
    instant_book       = Column(Boolean, nullable=False, default=True)
    status             = Column(Enum(TripStatus), nullable=False, default=TripStatus.active)
    created_at         = Column(DateTime, nullable=False, default=datetime.utcnow)

    driver   = relationship("User",    back_populates="trips")
    bookings = relationship("Booking", back_populates="trip",
                            cascade="all, delete-orphan")
    reviews  = relationship("Review",  back_populates="trip",
                            cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("price_per_seat > 0",                      name="ck_trips_price"),
        CheckConstraint("seats_total >= 1 AND seats_total <= 8",   name="ck_trips_seats"),
        CheckConstraint("seats_available >= 0",                    name="ck_trips_seats_avail"),
        Index("ix_trips_driver_id",  "driver_id"),
        Index("ix_trips_origin",     "origin"),
        Index("ix_trips_destination","destination"),
        Index("ix_trips_departure",  "departure_datetime"),
        Index("ix_trips_status",     "status"),
    )

    @property
    def confirmed_passengers(self) -> int:
        return sum(b.seats_booked for b in self.bookings
                   if b.status == BookingStatus.confirmed)

    @property
    def average_rating(self) -> float | None:
        ratings = [r.rating for r in self.reviews
                   if r.review_type == ReviewType.passenger_to_driver]
        if not ratings:
            return None
        return round(sum(ratings) / len(ratings), 1)

    def __repr__(self) -> str:
        return f"<Trip id={self.id} {self.origin}→{self.destination}>"


# ── Bookings ──────────────────────────────────────────────────────────────────

class Booking(Base):
    __tablename__ = "bookings"

    id           = Column(Integer, primary_key=True)
    trip_id      = Column(Integer, ForeignKey("trips.id",  ondelete="CASCADE"),
                          nullable=False)
    passenger_id = Column(Integer, ForeignKey("users.id",  ondelete="CASCADE"),
                          nullable=False)
    seats_booked = Column(SmallInteger, nullable=False, default=1)
    total_price  = Column(Integer, nullable=False)
    service_fee  = Column(Integer, nullable=False, default=0)
    message      = Column(Text)
    status       = Column(Enum(BookingStatus), nullable=False,
                          default=BookingStatus.pending)
    created_at   = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at   = Column(DateTime, nullable=False, default=datetime.utcnow,
                          onupdate=datetime.utcnow)

    trip      = relationship("Trip", back_populates="bookings")
    passenger = relationship("User", back_populates="bookings",
                             foreign_keys=[passenger_id])
    review    = relationship("Review", back_populates="booking",
                             uselist=False, cascade="all, delete-orphan")
    payment   = relationship("Payment", back_populates="booking",
                             uselist=False, cascade="all, delete-orphan")
    messages  = relationship("Message", back_populates="booking",
                             cascade="all, delete-orphan",
                             order_by="Message.created_at")

    __table_args__ = (
        CheckConstraint("total_price >= 0",                        name="ck_bookings_price"),
        CheckConstraint("seats_booked >= 1 AND seats_booked <= 8", name="ck_bookings_seats"),
        Index("ix_bookings_trip_id",      "trip_id"),
        Index("ix_bookings_passenger_id", "passenger_id"),
        Index("ix_bookings_status",       "status"),
    )

    @property
    def subtotal(self) -> int:
        return self.total_price - self.service_fee

    def __repr__(self) -> str:
        return (f"<Booking id={self.id} trip_id={self.trip_id} "
                f"passenger_id={self.passenger_id} status={self.status}>")


# ── Reviews ───────────────────────────────────────────────────────────────────

class Review(Base):
    __tablename__ = "reviews"

    id          = Column(Integer, primary_key=True)
    booking_id  = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"),
                         nullable=False)
    trip_id     = Column(Integer, ForeignKey("trips.id",    ondelete="CASCADE"),
                         nullable=False)
    reviewer_id = Column(Integer, ForeignKey("users.id",    ondelete="CASCADE"),
                         nullable=False)
    reviewee_id = Column(Integer, ForeignKey("users.id",    ondelete="CASCADE"),
                         nullable=False)
    review_type = Column(Enum(ReviewType), nullable=False)
    rating      = Column(SmallInteger, nullable=False)
    comment     = Column(Text)
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)

    booking  = relationship("Booking", back_populates="review")
    trip     = relationship("Trip",    back_populates="reviews")
    reviewer = relationship("User",    back_populates="reviews_given",
                            foreign_keys=[reviewer_id])
    reviewee = relationship("User",    back_populates="reviews_received",
                            foreign_keys=[reviewee_id])

    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating"),
        UniqueConstraint("booking_id", "review_type",  name="uq_review_per_booking"),
        Index("ix_reviews_booking_id",  "booking_id"),
        Index("ix_reviews_trip_id",     "trip_id"),
        Index("ix_reviews_reviewer_id", "reviewer_id"),
        Index("ix_reviews_reviewee_id", "reviewee_id"),
    )

    def __repr__(self) -> str:
        return f"<Review id={self.id} type={self.review_type} rating={self.rating}>"


# ── Payments ──────────────────────────────────────────────────────────────────

class Payment(Base):
    """
    One payment record per booking.
    Mirrors BlaBlaCar Germany's cost-sharing model:
      - Passenger pays: contribution + service fee
      - Driver receives: contribution only (service fee stays with platform)
      - Cancellation refunds governed by time-to-departure rules
    """
    __tablename__ = "payments"

    id                = Column(Integer, primary_key=True)
    booking_id        = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"),
                               nullable=False, unique=True)
    passenger_total   = Column(Integer, nullable=False)   # incl. service fee (ISK)
    driver_payout     = Column(Integer, nullable=False)   # contribution only (ISK)
    platform_fee      = Column(Integer, nullable=False)   # service fee (ISK)
    refund_amount     = Column(Integer, nullable=False, default=0)
    status            = Column(Enum(PaymentStatus), nullable=False,
                               default=PaymentStatus.authorised)
    # Masked card for display — never store real card data
    card_last4        = Column(String(4))
    card_brand        = Column(String(20))
    created_at        = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at        = Column(DateTime, nullable=False, default=datetime.utcnow,
                               onupdate=datetime.utcnow)

    booking = relationship("Booking", back_populates="payment")

    __table_args__ = (
        Index("ix_payments_booking_id", "booking_id"),
        Index("ix_payments_status",     "status"),
    )

    def __repr__(self) -> str:
        return f"<Payment id={self.id} booking_id={self.booking_id} status={self.status}>"


# ── Messages ──────────────────────────────────────────────────────────────────

class Message(Base):
    """
    One message in a conversation thread attached to a booking.
    The two participants are always: booking.passenger  ↔  booking.trip.driver
    """
    __tablename__ = "messages"

    id         = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"),
                        nullable=False)
    sender_id  = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False)
    body       = Column(Text, nullable=False)
    is_read    = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    booking = relationship("Booking", back_populates="messages")
    sender  = relationship("User",    back_populates="messages_sent",
                           foreign_keys=[sender_id])

    __table_args__ = (
        Index("ix_messages_booking_id", "booking_id"),
        Index("ix_messages_sender_id",  "sender_id"),
    )

    def __repr__(self) -> str:
        return f"<Message id={self.id} booking_id={self.booking_id} sender_id={self.sender_id}>"
