"""Account, profile and authentication payloads."""

import re
import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import (
    ActivityLevel,
    Goal,
    Sex,
    TrainingLevel,
    UnitSystem,
    UserRole,
)

PASSWORD_RULES = (
    "Use at least 10 characters with an uppercase letter, a lowercase letter and a number."
)


def _validate_password(value: str) -> str:
    if len(value) < 10:
        raise ValueError(PASSWORD_RULES)
    if not re.search(r"[A-Z]", value) or not re.search(r"[a-z]", value):
        raise ValueError(PASSWORD_RULES)
    if not re.search(r"\d", value):
        raise ValueError(PASSWORD_RULES)
    return value


class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(max_length=128)
    accepts_terms: bool = True

    _check_password = field_validator("password")(_validate_password)

    @field_validator("accepts_terms")
    @classmethod
    def _must_accept(cls, v: bool) -> bool:
        if not v:
            raise ValueError("You need to accept the terms to create an account.")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    password: str = Field(max_length=128)

    _check_password = field_validator("password")(_validate_password)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(max_length=128)

    _check_password = field_validator("new_password")(_validate_password)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ClientProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date_of_birth: date | None = None
    sex: Sex | None = None
    height_cm: float | None = None
    starting_weight_kg: float | None = None
    current_weight_kg: float | None = None
    goal_weight_kg: float | None = None
    goal: Goal
    activity_level: ActivityLevel
    unit_system: UnitSystem
    level: TrainingLevel | None = None
    phase: str | None = None
    program_start_date: date | None = None
    program_week: int
    program_total_weeks: int
    calorie_target: int | None = None
    protein_target_g: int | None = None
    carb_target_g: int | None = None
    fat_target_g: int | None = None
    weekly_workout_target: int
    sleep_target_hours: float
    weekly_cardio_target_min: int
    timezone: str
    medical_notes: str | None = None
    onboarding_completed: bool


class ClientProfileUpdate(BaseModel):
    date_of_birth: date | None = None
    sex: Sex | None = None
    height_cm: float | None = Field(default=None, gt=80, lt=260)
    current_weight_kg: float | None = Field(default=None, gt=25, lt=350)
    goal_weight_kg: float | None = Field(default=None, gt=25, lt=350)
    goal: Goal | None = None
    activity_level: ActivityLevel | None = None
    unit_system: UnitSystem | None = None
    timezone: str | None = Field(default=None, max_length=64)
    medical_notes: str | None = Field(default=None, max_length=2000)
    sleep_target_hours: float | None = Field(default=None, ge=4, le=12)
    weekly_cardio_target_min: int | None = Field(default=None, ge=0, le=2000)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    display_name: str | None = None
    role: UserRole
    avatar_url: str | None = None
    is_active: bool
    created_at: datetime
    profile: ClientProfileOut | None = None


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    display_name: str | None = Field(default=None, max_length=120)
    avatar_url: str | None = Field(default=None, max_length=500)