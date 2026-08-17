"""Nutrition, body tracking, sleep, cardio, calculators and engagement payloads."""

import uuid
from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import (
    ActivityLevel,
    CardioType,
    DataSource,
    Goal,
    Intensity,
    PhotoPose,
    Sex,
    TrainingLevel,
)

# --- Nutrition -----------------------------------------------------------------


class MealItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str


class MealOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    day_of_week: int
    order_index: int
    name: str
    serve_time: time | None = None
    icon: str | None = None
    calories: int
    protein_g: int
    carbs_g: int
    fat_g: int
    notes: str | None = None
    items: list[MealItemOut]


class MealPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    phase: Goal
    calorie_target: int
    protein_target_g: int
    carb_target_g: int
    fat_target_g: int
    notes: str | None = None
    meals: list[MealOut]


class MealLogIn(BaseModel):
    meal_id: uuid.UUID
    log_date: date | None = None
    is_completed: bool = True
    actual_calories: int | None = Field(default=None, ge=0, le=10000)


class MealLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    meal_id: uuid.UUID
    log_date: date
    is_completed: bool
    actual_calories: int | None = None


# --- Body tracking -------------------------------------------------------------


class WeightLogIn(BaseModel):
    log_date: date | None = None
    weight_kg: float = Field(gt=25, lt=350)
    note: str | None = Field(default=None, max_length=300)


class WeightLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    log_date: date
    weight_kg: float
    note: str | None = None


class MeasurementIn(BaseModel):
    log_date: date | None = None
    chest_cm: float | None = Field(default=None, gt=0, lt=250)
    waist_cm: float | None = Field(default=None, gt=0, lt=250)
    hips_cm: float | None = Field(default=None, gt=0, lt=250)
    left_arm_cm: float | None = Field(default=None, gt=0, lt=150)
    right_arm_cm: float | None = Field(default=None, gt=0, lt=150)
    left_thigh_cm: float | None = Field(default=None, gt=0, lt=200)
    right_thigh_cm: float | None = Field(default=None, gt=0, lt=200)
    neck_cm: float | None = Field(default=None, gt=0, lt=100)
    body_fat_pct: float | None = Field(default=None, ge=1, le=70)
    note: str | None = Field(default=None, max_length=300)


class MeasurementOut(MeasurementIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    log_date: date


class ProgressPhotoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    log_date: date
    pose: PhotoPose
    url: str
    note: str | None = None
    shared_with_coach: bool


# --- Sleep & cardio ------------------------------------------------------------


class SleepLogIn(BaseModel):
    log_date: date | None = None
    bedtime: time | None = None
    wake_time: time | None = None
    hours_slept: float = Field(ge=0, le=24)
    quality: int | None = Field(default=None, ge=1, le=5)
    awakenings: int | None = Field(default=None, ge=0, le=30)
    note: str | None = Field(default=None, max_length=1000)


class SleepLogOut(SleepLogIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    log_date: date
    hours_slept: float


class CardioLogIn(BaseModel):
    log_date: date | None = None
    activity_type: CardioType
    duration_minutes: int = Field(ge=1, le=1440)
    distance_km: float | None = Field(default=None, ge=0, le=500)
    avg_heart_rate: int | None = Field(default=None, ge=30, le=230)
    calories_burned: int | None = Field(default=None, ge=0, le=10000)
    intensity: Intensity = Intensity.MODERATE
    source: DataSource = DataSource.MANUAL
    note: str | None = Field(default=None, max_length=300)


class CardioLogOut(CardioLogIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    log_date: date


class WellnessSummary(BaseModel):
    avg_sleep_hours: float
    sleep_target_hours: float
    nights_logged: int
    avg_sleep_quality: float | None = None
    cardio_minutes: int
    cardio_target_minutes: int
    cardio_sessions: int
    cardio_calories: int
    top_activity: CardioType | None = None
    days_in_window: int


# --- Calculators ---------------------------------------------------------------


class CalorieRequest(BaseModel):
    age: int = Field(ge=13, le=100)
    sex: Sex
    weight_kg: float = Field(gt=25, lt=350)
    height_cm: float = Field(gt=80, lt=260)
    activity_level: ActivityLevel = ActivityLevel.LIGHT
    goal: Goal = Goal.MAINTAIN


class MacroSplit(BaseModel):
    protein_g: int
    carbs_g: int
    fat_g: int


class CalorieResponse(BaseModel):
    bmr: int
    tdee: int
    target_calories: int
    macros: MacroSplit
    formula: str = "Mifflin-St Jeor"
    note: str


class BmiRequest(BaseModel):
    weight_kg: float = Field(gt=25, lt=350)
    height_cm: float = Field(gt=80, lt=260)


class BmiResponse(BaseModel):
    bmi: float
    category: str
    healthy_weight_range_kg: tuple[float, float]
    note: str


class CardioBurnRequest(BaseModel):
    activity_type: CardioType
    duration_minutes: int = Field(ge=1, le=1440)
    weight_kg: float = Field(gt=25, lt=350)
    intensity: Intensity = Intensity.MODERATE


class CardioBurnResponse(BaseModel):
    calories_burned: int
    met_value: float
    note: str


# --- Engagement ----------------------------------------------------------------


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sender_id: uuid.UUID
    body: str
    read_at: datetime | None = None
    created_at: datetime


class ThreadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject: str
    last_message_at: datetime | None = None
    messages: list[MessageOut] = []


class BookingIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    preferred_at: datetime
    timezone: str = Field(default="America/Chicago", max_length=64)
    topic: str | None = Field(default=None, max_length=200)


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    preferred_at: datetime
    timezone: str
    topic: str | None = None
    status: str


class LeadIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    level_interest: TrainingLevel | None = None
    primary_goal: str | None = Field(default=None, max_length=120)
    message: str | None = Field(default=None, max_length=2000)
    consent_marketing: bool = False
    # Honeypot: real people leave this empty, bots fill it in.
    website: str | None = Field(default=None, max_length=200)


# --- Dashboard -----------------------------------------------------------------


class DashboardTrend(BaseModel):
    label: str
    value: float


class DashboardOut(BaseModel):
    greeting_name: str
    level: TrainingLevel
    phase: str | None
    program_week: int
    program_total_weeks: int
    current_weight_kg: float | None
    weight_change_kg: float | None
    calories_today: int
    calorie_target: int | None
    workouts_done_this_week: int
    weekly_workout_target: int
    weekly_streak: int
    sleep_last_night_hours: float | None
    cardio_minutes_this_week: int
    cardio_target_minutes: int
    weight_trend: list[DashboardTrend]
    today_day_id: uuid.UUID | None
    unread_messages: int
