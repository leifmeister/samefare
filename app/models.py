from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, Enum, Float,
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
    card_saved       = "card_saved"        # Case B: card tokenised, MIT scheduled for ride_date-24h
    pending          = "pending"
    confirmed        = "confirmed"
    rejected         = "rejected"
    cancelled        = "cancelled"
    completed        = "completed"
    no_show          = "no_show"   # passenger did not appear — reported by driver


class PaymentStatus(_StrEnum):
    # Pre-Rapyd states
    pending        = "pending"           # payment attempt created, Rapyd action not yet taken
    card_saved     = "card_saved"        # Case B: SCA-authenticated CIT done, MIT scheduled
    # Rapyd authorisation states
    authorised         = "authorised"          # card authorised (ACT), capture pending
    capture_requested  = "capture_requested"   # capture POST sent; awaiting PAYMENT_CAPTURED webhook
    # Terminal states
    captured       = "captured"          # funds captured — confirmed by Rapyd (CLO / PAYMENT_CAPTURED)
    # Refund lifecycle — three distinct states so reconciliation is unambiguous:
    #   refund_requested → Rapyd call in-flight or interrupted before response
    #   refund_failed    → Rapyd returned an error; retry task will re-attempt
    #   refunded         → Rapyd confirmed full refund; passenger whole
    #   partial_refund   → Rapyd confirmed partial refund (service fee retained)
    refund_requested = "refund_requested"  # intent recorded, Rapyd call not yet confirmed
    refund_failed    = "refund_failed"     # Rapyd returned an error — needs retry
    refunded       = "refunded"            # full refund confirmed by Rapyd
    partial_refund = "partial_refund"      # partial refund confirmed by Rapyd
    failed         = "failed"              # authorisation or capture failed permanently
    auth_expired   = "auth_expired"        # authorisation lapsed before capture
    # Case B retry
    retry_pending  = "retry_pending"       # MIT failed; passenger has 2 h to update card (+5 % fee)


class ReviewType(_StrEnum):
    passenger_to_driver = "passenger_to_driver"
    driver_to_passenger = "driver_to_passenger"


class ReportReason(_StrEnum):
    harassment = "harassment"
    safety     = "safety"
    fraud      = "fraud"
    no_show    = "no_show"
    spam       = "spam"
    other      = "other"


class VerificationStatus(_StrEnum):
    unverified = "unverified"
    pending    = "pending"
    approved   = "approved"
    rejected   = "rejected"


class PayoutMethod(_StrEnum):
    blikk          = "blikk"           # Icelandic real-time bank transfer (IS IBAN)
    stripe_connect = "stripe_connect"  # Stripe Connect for non-Icelandic accounts


class PayoutItemStatus(_StrEnum):
    pending          = "pending"           # captured, waiting for terminal booking state or bank details
    payout_ready     = "payout_ready"      # eligible to batch into a DriverPayout
    payout_sent      = "payout_sent"       # included in a DriverPayout that was submitted
    payout_confirmed = "payout_confirmed"  # provider confirmed receipt
    payout_failed    = "payout_failed"     # provider rejected — see DriverPayout.failure_reason
    retry_ready      = "retry_ready"       # re-queued after a failed send
    reversed         = "reversed"          # refund issued after payout; offset required
    cancelled        = "cancelled"         # booking refunded/cancelled before payout was sent


class DriverPayoutStatus(_StrEnum):
    pending   = "pending"    # batch created, not yet submitted
    sent      = "sent"       # submitted to provider
    confirmed = "confirmed"  # provider confirmed receipt
    failed    = "failed"     # provider rejected
    reversed  = "reversed"   # payout reversed by provider or manually


class FuelType(_StrEnum):
    petrol   = "petrol"
    diesel   = "diesel"
    electric = "electric"
    hybrid   = "hybrid"


class LedgerEntryType(_StrEnum):
    driver_payable_created    = "driver_payable_created"     # PayoutItem created
    platform_fee_retained     = "platform_fee_retained"      # SameFare cut booked
    driver_payout_ready       = "driver_payout_ready"        # item advanced to payout_ready
    driver_payout_batched     = "driver_payout_batched"      # item included in DriverPayout
    driver_payout_sent        = "driver_payout_sent"         # batch submitted to provider
    driver_payout_confirmed   = "driver_payout_confirmed"    # provider confirmed
    driver_payout_failed      = "driver_payout_failed"       # provider rejected
    driver_payout_reversed    = "driver_payout_reversed"     # reversal posted
    driver_balance_adjustment = "driver_balance_adjustment"  # manual correction
    payout_item_cancelled     = "payout_item_cancelled"      # item voided (refund/cancellation)
    passenger_refund_confirmed = "passenger_refund_confirmed"  # Rapyd confirmed outbound refund


# ── Users ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True)
    email           = Column(String(255), unique=True, nullable=False)
    full_name       = Column(String(255), nullable=False)
    phone           = Column(String(50))
    hashed_password = Column(String(255), nullable=False)
    birth_year      = Column(Integer, nullable=True)
    role            = Column(Enum(UserRole), nullable=False, default=UserRole.both)
    is_active          = Column(Boolean, nullable=False, default=True)
    is_admin           = Column(Boolean, nullable=False, default=False)
    suspension_reason  = Column(String(500), nullable=True)
    deleted_at         = Column(DateTime,    nullable=True)
    avatar_url      = Column(String(512))
    bio             = Column(Text)
    created_at      = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at      = Column(DateTime, nullable=False, default=datetime.utcnow,
                             onupdate=datetime.utcnow)

    # Saved vehicle (pre-fills trip creation form)
    default_car_make  = Column(String(100))
    default_car_model = Column(String(100))
    default_car_year  = Column(Integer)
    default_car_type  = Column(Enum(CarType), nullable=False, default=CarType.sedan)

    # Phone verification
    phone_verified      = Column(Boolean, nullable=False, default=False)
    phone_otp           = Column(String(6))
    phone_otp_expires   = Column(DateTime)

    # Email verification
    email_verified       = Column(Boolean, nullable=False, default=False)
    email_verify_token   = Column(String(64))

    # Password reset
    reset_token         = Column(String(64))
    reset_token_expires = Column(DateTime)

    # Payout configuration
    # Drivers with an Icelandic bank account (IS IBAN) are paid via Blikk.
    # Others set up a Stripe Connect account and receive payouts in their local currency
    # (with FX fees applied by Stripe — their choice, disclosed at onboarding).
    payout_method      = Column(Enum(PayoutMethod), nullable=True)
    blikk_account_iban = Column(String(34), nullable=True)   # e.g. IS14 0159 2600 7654 5510 7303 (IBAN)
    stripe_account_id  = Column(String(255), nullable=True)  # Stripe Connect acct_xxx ID

    # Identity & licence verification
    id_verification          = Column(Enum(VerificationStatus), nullable=False,
                                      default=VerificationStatus.unverified)
    license_verification     = Column(Enum(VerificationStatus), nullable=False,
                                      default=VerificationStatus.unverified)
    id_doc_filename          = Column(String(255))
    id_doc_type              = Column(String(20))   # 'license', 'passport', 'national_id'
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
        """
        Driver rating: average of passenger_to_driver reviews only.
        Shown on trip cards, checkout, and public profiles so passengers
        see a signal based solely on how this person drives — not mixed
        with reviews they received as a passenger on someone else's trip.
        """
        ratings = [
            r.rating for r in self.reviews_received
            if r.review_type == ReviewType.passenger_to_driver
        ]
        if not ratings:
            return None
        return round(sum(ratings) / len(ratings), 1)

    @property
    def passenger_rating(self) -> float | None:
        """
        Passenger rating: average of driver_to_passenger reviews only.
        Kept separate so drivers can eventually surface it without
        polluting the driver trust signal above.
        """
        ratings = [
            r.rating for r in self.reviews_received
            if r.review_type == ReviewType.driver_to_passenger
        ]
        if not ratings:
            return None
        return round(sum(ratings) / len(ratings), 1)

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
    large_luggage      = Column(Boolean, nullable=False, default=False)  # skis, bikes, airport bags
    allows_pets        = Column(Boolean, nullable=False, default=False)
    smoking            = Column(Boolean, nullable=False, default=False)
    chattiness         = Column(String(10), nullable=True)  # 'quiet', 'chatty', or NULL (no preference)
    winter_ready       = Column(Boolean, nullable=False, default=False)  # 4WD, snow tyres
    child_seat         = Column(Boolean, nullable=False, default=False)  # child seat available
    flexible_pickup    = Column(Boolean, nullable=False, default=False)  # can adjust meeting point
    instant_book       = Column(Boolean, nullable=False, default=True)
    allow_segments     = Column(Boolean, nullable=False, default=False)  # allow partial-route bookings
    driver_no_show     = Column(Boolean, nullable=False, default=False)  # reported by a passenger
    reminder_sent      = Column(Boolean, nullable=False, default=False)  # day-before SMS reminder fired
    status             = Column(Enum(TripStatus), nullable=False, default=TripStatus.active)
    created_at         = Column(DateTime, nullable=False, default=datetime.utcnow)

    # ── Pricing module ─────────────────────────────────────────────────────────
    # fuel_type: petrol/diesel/electric/hybrid — used by the cost estimator.
    # NULL on old trips; inferred from car_type ('electric' → electric, else petrol).
    fuel_type      = Column(Enum(FuelType), nullable=True)
    # price_snapshot: JSON-serialised TripCostEstimate stored at trip creation.
    # Allows exact reproduction of the cap calculation for any historical trip.
    price_snapshot = Column(Text, nullable=True)

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
    # Segment booking: passenger boards/exits at a city that differs from the
    # driver's trip origin/destination.  NULL means use the trip's own city.
    pickup_city  = Column(String(150), nullable=True)   # null → trip.origin
    dropoff_city = Column(String(150), nullable=True)   # null → trip.destination
    status           = Column(Enum(BookingStatus), nullable=False,
                              default=BookingStatus.pending)
    payment_deadline = Column(DateTime, nullable=True)   # set when status→awaiting_payment
    created_at       = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at       = Column(DateTime, nullable=False, default=datetime.utcnow,
                              onupdate=datetime.utcnow)

    trip      = relationship("Trip", back_populates="bookings")
    passenger = relationship("User", back_populates="bookings",
                             foreign_keys=[passenger_id])
    reviews   = relationship("Review", back_populates="booking",
                             cascade="all, delete-orphan")
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
    is_auto     = Column(Boolean, nullable=False, default=False)
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)

    booking  = relationship("Booking", back_populates="reviews")
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
                               default=PaymentStatus.pending)
    # Masked card for display — never store raw card data
    card_last4        = Column(String(4))
    card_brand        = Column(String(20))
    created_at        = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at        = Column(DateTime, nullable=False, default=datetime.utcnow,
                               onupdate=datetime.utcnow)

    # ── Rapyd integration ────────────────────────────────────────────────────
    rapyd_payment_id        = Column(String(255), nullable=True)  # payment object ID (after auth)
    rapyd_customer_id       = Column(String(255), nullable=True)  # customer ID (Case B)
    rapyd_payment_method_id = Column(String(255), nullable=True)  # saved PM token (Case B)
    rapyd_checkout_id       = Column(String(255), nullable=True)  # checkout page ID

    # Idempotency key — generated once per payment attempt, reused for retries
    idempotency_key         = Column(String(64), nullable=True)

    # ── Timing ──────────────────────────────────────────────────────────────
    payment_case        = Column(String(1), nullable=True)   # 'A' or 'B'
    auth_expires_at     = Column(DateTime, nullable=True)    # when the 7-day auth window ends
    capture_at          = Column(DateTime, nullable=True)    # scheduled capture (= departure_datetime)
    auth_scheduled_for  = Column(DateTime, nullable=True)    # Case B: when to fire MIT (= departure - 24h)

    # ── Retry state (Case B MIT failure) ────────────────────────────────────
    retry_deadline      = Column(DateTime, nullable=True)    # 2 h after failed MIT
    retry_fee_applied   = Column(Boolean,  nullable=False, default=False)

    # ── Webhook deduplication ────────────────────────────────────────────────
    # JSON array of Rapyd webhook IDs already processed for this payment.
    seen_webhook_ids    = Column(Text, nullable=True)        # e.g. '["wh_abc","wh_def"]'

    booking     = relationship("Booking",    back_populates="payment")
    payout_item = relationship("PayoutItem", back_populates="payment", uselist=False)

    __table_args__ = (
        Index("ix_payments_booking_id", "booking_id"),
        Index("ix_payments_status",     "status"),
    )

    def __repr__(self) -> str:
        return f"<Payment id={self.id} booking_id={self.booking_id} status={self.status}>"


# ── User reports ─────────────────────────────────────────────────────────────

class UserReport(Base):
    __tablename__ = "user_reports"

    id          = Column(Integer, primary_key=True)
    reporter_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reported_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    booking_id  = Column(Integer, ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True)
    reason      = Column(Enum(ReportReason), nullable=False)
    comment     = Column(Text, nullable=True)
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)

    reporter = relationship("User",    foreign_keys=[reporter_id])
    reported = relationship("User",    foreign_keys=[reported_id])
    booking  = relationship("Booking", foreign_keys=[booking_id])

    __table_args__ = (
        Index("ix_user_reports_reported_id",  "reported_id"),
        Index("ix_user_reports_reporter_id",  "reporter_id"),
        Index("ix_user_reports_created_at",   "created_at"),
    )


# ── Newsletter ────────────────────────────────────────────────────────────────

class NewsletterSubscriber(Base):
    __tablename__ = "newsletter_subscribers"

    id             = Column(Integer, primary_key=True)
    email          = Column(String(255), unique=True, nullable=False)
    source         = Column(String(50))   # 'footer', 'homepage', 'registration'
    discount_used  = Column(Boolean, nullable=False, default=False)
    created_at     = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (Index("ix_newsletter_email", "email"),)

    def __repr__(self) -> str:
        return f"<NewsletterSubscriber email={self.email!r}>"


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


# ── Ride Alerts ───────────────────────────────────────────────────────────────

class RideAlert(Base):
    """
    A passenger's request to be notified when a matching ride appears.

    Works for both logged-in users (user_id set) and guests (email only).
    Each alert has a unique token so the owner can unsubscribe via an email link
    without needing to log in.
    """
    __tablename__ = "ride_alerts"

    id               = Column(Integer, primary_key=True)
    # Nullable — guests don't have an account
    user_id          = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                              nullable=True)
    email            = Column(String(255), nullable=False)
    origin           = Column(String(150), nullable=False)
    destination      = Column(String(150), nullable=False)
    # Optional — if set, only trips departing on this date trigger the alert
    travel_date      = Column(Date, nullable=True)
    seats            = Column(SmallInteger, nullable=False, default=1)
    token            = Column(String(64), unique=True, nullable=False)
    is_active        = Column(Boolean, nullable=False, default=True)
    last_notified_at = Column(DateTime, nullable=True)
    created_at       = Column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship("User", backref="ride_alerts", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_ride_alerts_email",   "email"),
        Index("ix_ride_alerts_user_id", "user_id"),
        Index("ix_ride_alerts_active",  "is_active"),
    )

    def __repr__(self) -> str:
        return (f"<RideAlert id={self.id} "
                f"{self.origin}→{self.destination} email={self.email!r}>")


# ── Payout ledger ─────────────────────────────────────────────────────────────
#
# Three-layer payout model:
#
#   Payment          — money collected from the passenger (Rapyd)
#   PayoutItem       — driver's entitlement from one captured payment (1-to-1)
#   DriverPayout     — outbound transfer batch through Blikk or Stripe Connect
#   PayoutLedgerEntry— immutable append-only accounting log
#
# Rule: Rapyd, Blikk, and Stripe are rails. This ledger is the source of truth.
# Never calculate "who should be paid" from Booking rows on the fly; always
# derive it from PayoutItem / PayoutLedgerEntry.


class DriverPayout(Base):
    """
    One outbound transfer (or batch of transfers) sent to a driver through
    Blikk or Stripe Connect.  A single DriverPayout may cover multiple
    PayoutItems (e.g. a driver with three passengers on one trip).

    Status flow: pending → sent → confirmed
                              ↘ failed → (manual retry creates new DriverPayout)
    """
    __tablename__ = "driver_payouts"

    id                 = Column(Integer, primary_key=True)
    driver_id          = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"),
                                nullable=False)

    amount             = Column(Integer, nullable=False)               # total ISK to transfer
    currency           = Column(String(3), nullable=False, default="ISK")
    payout_method      = Column(Enum(PayoutMethod), nullable=False)
    status             = Column(Enum(DriverPayoutStatus), nullable=False,
                                default=DriverPayoutStatus.pending)

    # Provider tracking
    idempotency_key    = Column(String(64), nullable=False, unique=True)
    provider_payout_id = Column(String(255), nullable=True)  # Blikk ref / Stripe transfer ID
    provider_response  = Column(Text, nullable=True)          # raw JSON from provider

    # Outcome timestamps
    sent_at            = Column(DateTime, nullable=True)
    confirmed_at       = Column(DateTime, nullable=True)
    failed_at          = Column(DateTime, nullable=True)
    failure_reason     = Column(Text, nullable=True)

    created_at         = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at         = Column(DateTime, nullable=False, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    driver = relationship("User", foreign_keys=[driver_id])
    items  = relationship("PayoutItem", back_populates="driver_payout",
                          foreign_keys="PayoutItem.driver_payout_id")

    __table_args__ = (
        Index("ix_driver_payouts_driver_id", "driver_id"),
        Index("ix_driver_payouts_status",    "status"),
    )

    def __repr__(self) -> str:
        return (f"<DriverPayout id={self.id} driver_id={self.driver_id} "
                f"amount={self.amount} status={self.status}>")


class PayoutItem(Base):
    """
    Pairs one captured passenger Payment to the driver's entitlement.
    The unique constraint on payment_id is the critical guard that prevents
    one passenger payment from funding two driver payouts.

    Status flow: pending → payout_ready → payout_sent → payout_confirmed
                                                       ↘ payout_failed → retry_ready
                 pending → cancelled  (refund before payout)
                 payout_confirmed → reversed  (refund after payout — needs offset)
    """
    __tablename__ = "payout_items"

    id               = Column(Integer, primary_key=True)
    payment_id       = Column(Integer, ForeignKey("payments.id",  ondelete="RESTRICT"),
                              nullable=False, unique=True)   # enforces 1 item per payment
    booking_id       = Column(Integer, ForeignKey("bookings.id",  ondelete="RESTRICT"),
                              nullable=False)
    driver_id        = Column(Integer, ForeignKey("users.id",     ondelete="RESTRICT"),
                              nullable=False)
    driver_payout_id = Column(Integer, ForeignKey("driver_payouts.id", ondelete="SET NULL"),
                              nullable=True)   # set when item is batched

    # Amounts snapshot at creation — never recalculate from live booking rows
    amount           = Column(Integer, nullable=False)   # driver's cut (ISK)
    platform_fee     = Column(Integer, nullable=False)   # SameFare's cut (ISK)
    passenger_total  = Column(Integer, nullable=False)   # total charged to passenger (ISK)

    payout_method    = Column(Enum(PayoutMethod), nullable=True)   # resolved from driver profile
    status           = Column(Enum(PayoutItemStatus), nullable=False,
                              default=PayoutItemStatus.pending)

    # Stable idempotency key for the outbound transfer call (derived, not random)
    idempotency_key  = Column(String(64), nullable=False, unique=True)

    created_at       = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at       = Column(DateTime, nullable=False, default=datetime.utcnow,
                              onupdate=datetime.utcnow)

    payment      = relationship("Payment",     back_populates="payout_item")
    booking      = relationship("Booking",     foreign_keys=[booking_id])
    driver       = relationship("User",        foreign_keys=[driver_id])
    driver_payout = relationship("DriverPayout", back_populates="items",
                                 foreign_keys=[driver_payout_id])

    __table_args__ = (
        Index("ix_payout_items_driver_id", "driver_id"),
        Index("ix_payout_items_status",    "status"),
    )

    def __repr__(self) -> str:
        return (f"<PayoutItem id={self.id} payment_id={self.payment_id} "
                f"amount={self.amount} status={self.status}>")


class PayoutLedgerEntry(Base):
    """
    Immutable, append-only accounting log.

    Rules:
    - Never UPDATE or DELETE ledger rows.
    - Post corrections as new rows with negative amounts or a specific
      reversal entry_type.
    - Every significant payout event writes at least one ledger row.
    """
    __tablename__ = "payout_ledger"

    id               = Column(Integer, primary_key=True)
    entry_type       = Column(Enum(LedgerEntryType), nullable=False)

    # Context FKs — all nullable; not every entry relates to every object
    payment_id       = Column(Integer, ForeignKey("payments.id"),       nullable=True)
    payout_item_id   = Column(Integer, ForeignKey("payout_items.id"),   nullable=True)
    driver_payout_id = Column(Integer, ForeignKey("driver_payouts.id"), nullable=True)
    booking_id       = Column(Integer, ForeignKey("bookings.id"),       nullable=True)
    driver_id        = Column(Integer, ForeignKey("users.id"),          nullable=True)

    amount           = Column(Integer, nullable=False)          # ISK; negative = reversal/debit
    currency         = Column(String(3), nullable=False, default="ISK")
    note             = Column(Text, nullable=True)

    created_at       = Column(DateTime, nullable=False, default=datetime.utcnow)

    # No cascade deletes — ledger entries are permanent historical records.
    # FKs point to their subjects for JOIN queries but do not own them.

    __table_args__ = (
        Index("ix_payout_ledger_driver_id",  "driver_id"),
        Index("ix_payout_ledger_payment_id", "payment_id"),
        Index("ix_payout_ledger_entry_type", "entry_type"),
        Index("ix_payout_ledger_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return (f"<PayoutLedgerEntry id={self.id} type={self.entry_type} "
                f"amount={self.amount} driver_id={self.driver_id}>")


# ── Pricing infrastructure ─────────────────────────────────────────────────────

class PricingPolicy(Base):
    """
    Versioned table of cost-estimation constants.

    One row per policy period.  The estimator picks the row where
    effective_from <= today and (effective_to IS NULL OR effective_to >= today),
    ordering by effective_from DESC to get the most recent active policy.

    This means a future rate change (e.g. new kílómetragjald rate) can be
    pre-entered with its future effective_from date and will apply automatically
    at midnight on that date — no deployment needed.
    """
    __tablename__ = "pricing_policy"

    id             = Column(Integer, primary_key=True)
    effective_from = Column(Date, nullable=False)
    effective_to   = Column(Date, nullable=True)   # NULL = open-ended (current)

    # ── Kílómetragjald (ISK/km) ───────────────────────────────────────────────
    # Statutory road-use charge, applies to all vehicles regardless of fuel type.
    # Source: island.is/kilometragjald — rate set annually in the budget law.
    # Heavier vehicles (>= 3.5 t) pay a higher band; most private cars use standard.
    kilometragjald_standard = Column(Float, nullable=False)  # < 3.5 t
    kilometragjald_heavy    = Column(Float, nullable=True)   # >= 3.5 t

    # ── Default fuel consumption by vehicle class (L/100 km) ──────────────────
    consumption_small    = Column(Float, nullable=False)   # small hatchback
    consumption_standard = Column(Float, nullable=False)   # sedan / standard
    consumption_suv      = Column(Float, nullable=False)   # SUV / 4x4
    consumption_van      = Column(Float, nullable=False)   # van / camper

    # ── Default EV consumption by vehicle class (kWh/100 km) ─────────────────
    ev_consumption_standard = Column(Float, nullable=False)
    ev_consumption_suv      = Column(Float, nullable=False)

    # ── Electricity price (ISK/kWh) ───────────────────────────────────────────
    electricity_price_isk_per_kwh = Column(Float, nullable=False)

    # ── Wear and tear (ISK/km) ────────────────────────────────────────────────
    # Conservative estimate covering tyres, brakes, filters, oil.
    wear_and_tear_isk_per_km = Column(Float, nullable=False)

    # ── Marginal depreciation ─────────────────────────────────────────────────
    # Only a fraction of real ownership depreciation is passed to passengers.
    # The driver already owns the car; we reimburse only the marginal usage cost.
    # allowed_depreciation = real_depreciation_isk_per_km * depreciation_factor
    real_depreciation_isk_per_km = Column(Float, nullable=False)
    depreciation_factor          = Column(Float, nullable=False)  # 0.0–1.0, e.g. 0.40

    # ── Platform cap (ISK/km) ─────────────────────────────────────────────────
    # Hard ceiling: allowed_cost_per_km = min(raw_cost, cap).
    # Prevents edge cases (heavy SUV + expensive diesel) from producing
    # unreasonable per-seat caps.
    platform_cost_cap_isk_per_km = Column(Float, nullable=False)

    # ── Rounding ──────────────────────────────────────────────────────────────
    rounding_unit = Column(Integer, nullable=False, default=50)  # ISK — always round DOWN

    # ── Fuel price guardrails ─────────────────────────────────────────────────
    # fallback: used when the live fetch and cache both fail.
    # min/max: sanity bounds — reject the fetched price if outside this range.
    fuel_price_fallback_isk_per_liter = Column(Float, nullable=False)
    fuel_price_min_isk_per_liter      = Column(Float, nullable=False)
    fuel_price_max_isk_per_liter      = Column(Float, nullable=False)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    notes      = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_pricing_policy_effective_from", "effective_from"),
    )

    def __repr__(self) -> str:
        return f"<PricingPolicy id={self.id} from={self.effective_from} to={self.effective_to}>"


class Route(Base):
    """
    Canonical city-pair route table.

    Distances are sourced from a routing API (or pre-seeded approximations
    marked source='seeded_approximate') and stored as a snapshot so that
    the estimator produces consistent results even if road distances change.

    last_verified_at=NULL means the distance has never been verified against
    a live routing API.  An admin task should periodically re-verify entries
    and update the distance and polyline.

    Both directions of a route are stored as separate rows (A→B and B→A),
    because road distances and durations can differ.
    """
    __tablename__ = "routes"

    id               = Column(Integer, primary_key=True)
    origin           = Column(String(150), nullable=False)
    destination      = Column(String(150), nullable=False)
    distance_km      = Column(Float,       nullable=False)
    duration_min     = Column(Integer,     nullable=True)
    polyline         = Column(Text,        nullable=True)   # encoded polyline for map
    source           = Column(String(50),  nullable=True)   # e.g. 'google_maps', 'seeded_approximate'
    last_verified_at = Column(DateTime,    nullable=True)
    is_active        = Column(Boolean,     nullable=False, default=True)
    created_at       = Column(DateTime,    nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("origin", "destination", name="uq_routes_origin_destination"),
        Index("ix_routes_origin",      "origin"),
        Index("ix_routes_destination", "destination"),
        Index("ix_routes_active",      "is_active"),
    )

    def __repr__(self) -> str:
        return f"<Route {self.origin}→{self.destination} {self.distance_km} km>"


class FuelPriceCache(Base):
    """
    Audit log of fetched fuel prices from apis.is.

    Each successful fetch inserts a new row; no rows are updated.
    The most recent row within MAX_CACHE_AGE_DAYS (7 days) is used
    as the cached price when the live fetch fails.
    """
    __tablename__ = "fuel_price_cache"

    id            = Column(Integer,  primary_key=True)
    fuel_type     = Column(String(20), nullable=False, default="petrol")
    p80_price     = Column(Float,    nullable=False)   # 80th percentile ISK/L
    median_price  = Column(Float,    nullable=True)    # stored for reference / admin
    station_count = Column(Integer,  nullable=True)    # number of stations in sample
    source        = Column(String(50), nullable=False, default="apis_is")
    fetched_at    = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_fuel_price_cache_fuel_type",  "fuel_type"),
        Index("ix_fuel_price_cache_fetched_at", "fetched_at"),
    )


# ── City suggestions ──────────────────────────────────────────────────────────

class CitySuggestion(Base):
    """User-submitted suggestions for cities/routes not yet in the network."""
    __tablename__ = "city_suggestions"

    id               = Column(Integer, primary_key=True)
    city_name        = Column(String(150), nullable=False)
    context_origin   = Column(String(150), nullable=True)   # what they searched from
    context_destination = Column(String(150), nullable=True)  # what they searched to
    suggested_by_id  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at       = Column(DateTime, nullable=False, default=datetime.utcnow)

    suggested_by = relationship("User", foreign_keys=[suggested_by_id])

    __table_args__ = (
        Index("ix_city_suggestions_city_name",  "city_name"),
        Index("ix_city_suggestions_created_at", "created_at"),
    )
