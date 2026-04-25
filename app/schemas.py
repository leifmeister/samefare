from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, field_validator, model_validator

from app.models import BookingStatus, CarType, TripStatus


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str
    phone: Optional[str] = None
    password: str
    confirm_password: str

    @model_validator(mode="after")
    def passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    bio: Optional[str] = None


class TripCreate(BaseModel):
    origin: str
    destination: str
    departure_datetime: datetime
    seats_total: int
    price_per_seat: int
    car_make: Optional[str] = None
    car_model: Optional[str] = None
    car_year: Optional[int] = None
    car_type: CarType = CarType.sedan
    description: Optional[str] = None
    allows_luggage: bool = True
    allows_pets: bool = False
    smoking: bool = False

    @field_validator("price_per_seat")
    @classmethod
    def price_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Price must be positive")
        return v

    @field_validator("seats_total")
    @classmethod
    def seats_valid(cls, v: int) -> int:
        if not 1 <= v <= 8:
            raise ValueError("Seats must be between 1 and 8")
        return v


class BookingCreate(BaseModel):
    trip_id: int
    seats_booked: int = 1
    message: Optional[str] = None

    @field_validator("seats_booked")
    @classmethod
    def seats_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Must book at least 1 seat")
        return v
