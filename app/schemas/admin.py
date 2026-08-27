"""
Payloads for the admin dashboard.

The dashboard is the only surface that can read another person's record, so the
read models here are deliberately explicit about what leaves the database.
"""

import uuid
from datetime import date, datetime, time

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from app.models.enums import (
    ActivityLevel,
    BookingStatus,
    Goal,
    LeadStatus,
    Sex,
    TrainingLevel,
    UnitSystem,
    UserRole,
)
from app.schemas.user import PASSWORD_RULES

# --- Roster -------------------------------------------------------------------


class ClientRow(BaseModel):
    """One line in the client list. Everything the coach triages on, nothing more."""

    id: uuid.UUID
    full_name: str
    display_name: str | None = None
    email: EmailStr
    avatar_url: str | None = None
    is_active: bool
    role: UserRole
    created_at: datetime
    last_login_at: datetime | None = None

    level: TrainingLevel | None = None
    goal: Goal | None = None
    phase: str | None = None
    program_week: int | None = None
    program_total_weeks: int | None = None
    current_weight_kg: float | None = None
    starting_weight_kg: float | None = None
    goal_weight_kg: float | None = None
    onboarding_completed: bool = False

    # Derived signals — these are what tell the coach who needs attention today.
    last_weight_log: date | None = None
    last_session_date: date | None = None
    sessions_last_7d: int = 0
    unread_from_client: int = 0
    has_active_plan: bool = False
    has_active_meal_plan: bool = False


class ClientPage(BaseModel):
    items: list[ClientRow]
    total: int
    limit: int
    offset: int


# --- Full client record -------------------------------------------------------


class MetricPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    log_date: date
    value: float


class MeasurementRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    log_date: date
    chest_cm: float | None = None
    waist_cm: float | None = None
    hips_cm: float | None = None
    left_arm_cm: float | None = None
    right_arm_cm: float | None = None
    left_thigh_cm: float | None = None
    right_thigh_cm: float | None = None
    neck_cm: float | None = None
    body_fat_pct: float | None = None
    note: str | None = None


class SleepRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    log_date: date
    hours_slept: float
    quality: int | None = None
    bedtime: time | None = None
    wake_time: time | None = None


class CardioRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    log_date: date
    activity_type: str
    duration_minutes: int
    distance_km: float | None = None
    avg_heart_rate: int | None = None
    calories_burned: int | None = None
    intensity: str


class SessionRow(BaseModel):
    id: uuid.UUID
    session_date: date
    status: str
    duration_minutes: int | None = None
    day_label: str | None = None
    focus: str | None = None
    set_count: int = 0
    volume_kg: float = 0


class PhotoRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    log_date: date
    pose: str
    url: str
    note: str | None = None


class ClientDetail(BaseModel):
    """One client's whole record, assembled in a single round trip."""

    account: ClientRow
    profile: dict
    adherence: dict
    weight_series: list[MetricPoint]
    measurements: list[MeasurementRow]
    sleep: list[SleepRow]
    cardio: list[CardioRow]
    sessions: list[SessionRow]
    photos: list[PhotoRow]


# --- Account and profile writes ----------------------------------------------


class ClientCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(max_length=128)
    role: UserRole = UserRole.CLIENT
    level: TrainingLevel = TrainingLevel.LEVEL_1
    send_welcome: bool = True

    @field_validator("password")
    @classmethod
    def _strong_enough(cls, value: str) -> str:
        import re

        if (
            len(value) < 10
            or not re.search(r"[A-Z]", value)
            or not re.search(r"[a-z]", value)
            or not re.search(r"\d", value)
        ):
            raise ValueError(PASSWORD_RULES)
        return value


class ClientAccountUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    display_name: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = None
    is_active: bool | None = None
    role: UserRole | None = None


class CoachProfileUpdate(BaseModel):
    """Everything the coach may set on a client's record.

    Wider than the client's own `ClientProfileUpdate`: the coach owns level,
    phase, macro targets and the private coaching notes.
    """

    date_of_birth: date | None = None
    sex: Sex | None = None
    height_cm: float | None = Field(default=None, gt=80, lt=260)
    starting_weight_kg: float | None = Field(default=None, gt=25, lt=350)
    current_weight_kg: float | None = Field(default=None, gt=25, lt=350)
    goal_weight_kg: float | None = Field(default=None, gt=25, lt=350)
    goal: Goal | None = None
    activity_level: ActivityLevel | None = None
    unit_system: UnitSystem | None = None

    level: TrainingLevel | None = None
    phase: str | None = Field(default=None, max_length=60)
    program_start_date: date | None = None
    program_week: int | None = Field(default=None, ge=1, le=104)
    program_total_weeks: int | None = Field(default=None, ge=1, le=104)

    calorie_target: int | None = Field(default=None, ge=800, le=6000)
    protein_target_g: int | None = Field(default=None, ge=0, le=500)
    carb_target_g: int | None = Field(default=None, ge=0, le=900)
    fat_target_g: int | None = Field(default=None, ge=0, le=300)
    weekly_workout_target: int | None = Field(default=None, ge=0, le=14)
    sleep_target_hours: float | None = Field(default=None, ge=4, le=12)
    weekly_cardio_target_min: int | None = Field(default=None, ge=0, le=2000)

    timezone: str | None = Field(default=None, max_length=64)
    medical_notes: str | None = Field(default=None, max_length=4000)
    coach_notes: str | None = Field(default=None, max_length=6000)


# --- Training plans -----------------------------------------------------------


class PlanExerciseIn(BaseModel):
    exercise_id: uuid.UUID
    # A demonstration for *this* client's version of the movement, overriding
    # the library's. Optional because the library link covers nearly every
    # case; when both are absent the save is refused outright — see
    # `app.services.programming.assert_every_movement_has_video`.
    video_url: HttpUrl | None = None
    sets: int = Field(default=3, ge=1, le=12)
    rep_range: str = Field(default="8-12", max_length=30)
    rest_seconds: int = Field(default=60, ge=0, le=900)
    tempo: str | None = Field(default=None, max_length=20)
    target_weight_kg: float | None = Field(default=None, ge=0, le=600)
    coach_note: str | None = Field(default=None, max_length=300)


class PlanDayIn(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    focus: str = Field(min_length=1, max_length=80)
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    estimated_minutes: int = Field(default=55, ge=10, le=240)
    exercises: list[PlanExerciseIn] = Field(default_factory=list, max_length=30)


class WorkoutPlanIn(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    level: TrainingLevel = TrainingLevel.LEVEL_1
    week_number: int = Field(default=1, ge=1, le=104)
    total_weeks: int = Field(default=12, ge=1, le=104)
    notes: str | None = Field(default=None, max_length=4000)
    program_id: uuid.UUID | None = None
    is_active: bool = True
    days: list[PlanDayIn] = Field(default_factory=list, max_length=14)


class PlanExerciseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    exercise_id: uuid.UUID
    exercise_name: str
    target_muscle: str
    order_index: int
    sets: int
    rep_range: str
    rest_seconds: int
    tempo: str | None = None
    target_weight_kg: float | None = None
    coach_note: str | None = None
    # Always populated on a saved plan. The write path refuses anything that
    # cannot resolve one, so the client portal can render the "watch this"
    # button unconditionally rather than hiding it half the time.
    video_url: str | None = None


class PlanDayOut(BaseModel):
    id: uuid.UUID
    label: str
    focus: str
    day_of_week: int | None = None
    order_index: int
    estimated_minutes: int
    exercises: list[PlanExerciseOut]


class WorkoutPlanOut(BaseModel):
    id: uuid.UUID
    name: str
    level: TrainingLevel
    week_number: int
    total_weeks: int
    notes: str | None = None
    is_custom: bool
    is_active: bool
    created_at: datetime
    days: list[PlanDayOut]


# --- Meal plans ---------------------------------------------------------------


class MealIn(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    name: str = Field(min_length=1, max_length=120)
    serve_time: time | None = None
    icon: str | None = Field(default=None, max_length=16)
    calories: int = Field(default=0, ge=0, le=5000)
    protein_g: int = Field(default=0, ge=0, le=400)
    carbs_g: int = Field(default=0, ge=0, le=800)
    fat_g: int = Field(default=0, ge=0, le=300)
    notes: str | None = Field(default=None, max_length=2000)
    items: list[str] = Field(default_factory=list, max_length=25)


class MealPlanIn(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    phase: Goal = Goal.CUT
    calorie_target: int = Field(ge=800, le=6000)
    protein_target_g: int = Field(ge=0, le=500)
    carb_target_g: int = Field(ge=0, le=900)
    fat_target_g: int = Field(ge=0, le=300)
    notes: str | None = Field(default=None, max_length=4000)
    is_active: bool = True
    meals: list[MealIn] = Field(default_factory=list, max_length=70)


class MealOut(BaseModel):
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
    items: list[str]


class MealPlanOut(BaseModel):
    id: uuid.UUID
    name: str
    phase: Goal
    calorie_target: int
    protein_target_g: int
    carb_target_g: int
    fat_target_g: int
    notes: str | None = None
    is_active: bool
    created_at: datetime
    meals: list[MealOut]


# --- Pricing plans (programs) -------------------------------------------------


class ProgramIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    level: TrainingLevel
    tagline: str = Field(min_length=2, max_length=200)
    days_per_week: int = Field(ge=1, le=7)
    session_minutes: int = Field(default=55, ge=10, le=240)
    price_cents: int = Field(ge=0, le=10_000_000)
    billing_period: str = Field(default="month", max_length=20)
    description: str = Field(min_length=2, max_length=6000)
    features: list[str] = Field(default_factory=list, max_length=20)
    best_for: str | None = Field(default=None, max_length=200)
    image_external_url: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    is_accepting_clients: bool = True
    sort_order: int = Field(default=0, ge=0, le=999)


class ProgramUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    level: TrainingLevel | None = None
    tagline: str | None = Field(default=None, min_length=2, max_length=200)
    days_per_week: int | None = Field(default=None, ge=1, le=7)
    session_minutes: int | None = Field(default=None, ge=10, le=240)
    price_cents: int | None = Field(default=None, ge=0, le=10_000_000)
    billing_period: str | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, min_length=2, max_length=6000)
    features: list[str] | None = Field(default=None, max_length=20)
    best_for: str | None = Field(default=None, max_length=200)
    image_external_url: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    is_accepting_clients: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=999)


class ProgramAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    level: TrainingLevel
    tagline: str
    days_per_week: int
    session_minutes: int
    price_cents: int
    billing_period: str
    description: str
    features: list[str]
    best_for: str | None = None
    image_url: str | None = None
    is_active: bool
    is_accepting_clients: bool
    sort_order: int
    client_count: int = 0
    created_at: datetime


# --- Inbox --------------------------------------------------------------------


class ThreadAttachment(BaseModel):
    """An image the client sent, addressed by a short-lived signed URL."""

    id: uuid.UUID
    url: str
    content_type: str
    file_size_bytes: int
    width: int | None = None
    height: int | None = None
    original_name: str | None = None


class ThreadMessage(BaseModel):
    id: uuid.UUID
    body: str
    created_at: datetime
    read_at: datetime | None = None
    from_coach: bool
    attachments: list[ThreadAttachment] = []


class ThreadOut(BaseModel):
    thread_id: uuid.UUID
    client_id: uuid.UUID
    client_name: str
    subject: str
    messages: list[ThreadMessage]


class ThreadSummary(BaseModel):
    thread_id: uuid.UUID
    client_id: uuid.UUID
    client_name: str
    avatar_url: str | None = None
    subject: str
    last_message_at: datetime | None = None
    preview: str | None = None
    unread: int = 0
    has_attachments: bool = False


class MessageIn(BaseModel):
    body: str = Field(default="", max_length=5000)
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _not_empty(self) -> "MessageIn":
        if not self.body.strip() and not self.attachment_ids:
            raise ValueError("Write something, or attach an image.")
        return self

    @field_validator("attachment_ids")
    @classmethod
    def _unique(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        seen: list[uuid.UUID] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return seen



class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    email: EmailStr
    phone: str | None = None
    level_interest: TrainingLevel | None = None
    primary_goal: str | None = None
    message: str | None = None
    source: str
    status: LeadStatus
    consent_marketing: bool
    created_at: datetime


class LeadUpdate(BaseModel):
    status: LeadStatus


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: uuid.UUID | None = None
    name: str
    email: EmailStr
    phone: str | None = None
    preferred_at: datetime
    timezone: str
    topic: str | None = None
    status: BookingStatus
    coach_notes: str | None = None
    created_at: datetime


class BookingUpdate(BaseModel):
    status: BookingStatus | None = None
    coach_notes: str | None = Field(default=None, max_length=4000)


# --- Overview -----------------------------------------------------------------


class OverviewCounts(BaseModel):
    active_clients: int
    inactive_clients: int
    new_clients_30d: int
    new_leads: int
    pending_bookings: int
    unread_messages: int
    published_tutorials: int
    active_programs: int


class AttentionItem(BaseModel):
    client_id: uuid.UUID
    client_name: str
    reason: str
    days: int | None = None


class OverviewOut(BaseModel):
    counts: OverviewCounts
    signups: list[MetricPoint]
    sessions: list[MetricPoint]
    needs_attention: list[AttentionItem]
    recent_leads: list[LeadOut]