"""Workout plan, session and set-logging payloads."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    Equipment,
    Goal,
    SessionStatus,
    Sex,
    TrainingExperience,
    TrainingLevel,
    TrainingLocation,
    UnitSystem,
)
from app.schemas.catalog import ExerciseOut


class DayExerciseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_index: int
    sets: int
    rep_range: str
    rest_seconds: int
    tempo: str | None = None
    target_weight_kg: float | None = None
    coach_note: str | None = None
    exercise: ExerciseOut


class WorkoutDayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    focus: str
    day_of_week: int | None = None
    order_index: int
    estimated_minutes: int
    exercises: list[DayExerciseOut]


class WorkoutPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    level: TrainingLevel
    week_number: int
    total_weeks: int
    notes: str | None = None
    is_custom: bool
    # "auto" (built from the client's intake) or "manual" (the coach's). The
    # portal shows it, so a client always knows which they are looking at.
    source: str = "manual"
    days: list[WorkoutDayOut]


# --- Training intake ----------------------------------------------------------
#
# One form, two jobs: it is the client's profile *and* the input to the
# automatic plan builder. Everything here is asked in plain language on
# /portal/workout and validated at this boundary, so a plan is never built
# from a number nobody could have typed.


class IntakeIn(BaseModel):
    """What the client fills in before their first automatic plan."""

    # Body — the plan and the macros are both sized from these.
    height_cm: float = Field(ge=90, le=250, description="Height in centimetres")
    current_weight_kg: float = Field(ge=30, le=300, description="Body weight in kilograms")
    goal_weight_kg: float | None = Field(default=None, ge=30, le=300)
    date_of_birth: date | None = None
    sex: Sex | None = None
    unit_system: UnitSystem | None = None

    # Training — what the block is actually built from.
    goal: Goal
    training_location: TrainingLocation
    training_experience: TrainingExperience
    # Empty means "whatever is normally in that place": everything in a gym,
    # bodyweight and bands at home. Unknown values are ignored rather than
    # rejected, so an older client build can never be locked out of the form.
    available_equipment: list[Equipment] = Field(default_factory=list, max_length=30)
    days_per_week: int = Field(ge=2, le=6)
    session_minutes: int = Field(ge=20, le=120)

    # Anything that should change what gets prescribed. Free text on purpose:
    # a knee that hurts on lunges does not fit a checkbox.
    medical_notes: str | None = Field(default=None, max_length=1000)


class IntakeOut(BaseModel):
    """The saved answers, plus what the portal needs to render the form."""

    model_config = ConfigDict(from_attributes=True)

    is_complete: bool
    completed_at: datetime | None = None
    height_cm: float | None = None
    current_weight_kg: float | None = None
    goal_weight_kg: float | None = None
    date_of_birth: date | None = None
    sex: Sex | None = None
    unit_system: UnitSystem | None = None
    goal: Goal | None = None
    training_location: TrainingLocation | None = None
    training_experience: TrainingExperience | None = None
    available_equipment: list[str] = Field(default_factory=list)
    days_per_week: int | None = None
    session_minutes: int | None = None
    medical_notes: str | None = None


class EquipmentOption(BaseModel):
    value: Equipment
    label: str


class IntakeFormOut(BaseModel):
    """Answers and options together: one request fills the whole screen."""

    intake: IntakeOut
    equipment_options: list[EquipmentOption]
    has_plan: bool
    plan_source: str | None = None


class IntakeResultOut(BaseModel):
    """The answer to "I filled in the form": the plan it produced."""

    intake: IntakeOut
    plan: WorkoutPlanOut | None = None
    # Populated when the intake saved but no plan could be built (no movement
    # in the library matches that equipment, say), so the portal can say why
    # instead of showing an empty Workout tab.
    message: str | None = None


class SetLogIn(BaseModel):
    day_exercise_id: uuid.UUID
    set_number: int = Field(ge=1, le=20)
    weight_kg: float | None = Field(default=None, ge=0, le=800)
    reps: int | None = Field(default=None, ge=0, le=500)
    rpe: float | None = Field(default=None, ge=1, le=10)
    is_completed: bool = True


class SetLogOut(SetLogIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID


class SessionStart(BaseModel):
    day_id: uuid.UUID
    session_date: date | None = None


class SessionUpdate(BaseModel):
    status: SessionStatus | None = None
    duration_minutes: int | None = Field(default=None, ge=0, le=600)
    calories_burned: int | None = Field(default=None, ge=0, le=5000)
    client_notes: str | None = Field(default=None, max_length=2000)


class WorkoutSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    day_id: uuid.UUID
    session_date: date
    status: SessionStatus
    duration_minutes: int | None = None
    calories_burned: int | None = None
    client_notes: str | None = None
    sets: list[SetLogOut] = []


# --- Client-built ("my own workout") plans -------------------------------------


class CustomDayExerciseIn(BaseModel):
    exercise_id: uuid.UUID
    sets: int = Field(default=3, ge=1, le=12)
    rep_range: str = Field(default="8-12", max_length=30)
    rest_seconds: int = Field(default=60, ge=0, le=600)
    coach_note: str | None = Field(default=None, max_length=300)


class CustomDayIn(BaseModel):
    label: str = Field(max_length=40)
    focus: str = Field(max_length=80)
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    exercises: list[CustomDayExerciseIn] = Field(min_length=1, max_length=20)


class CustomPlanIn(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    notes: str | None = Field(default=None, max_length=1000)
    days: list[CustomDayIn] = Field(min_length=1, max_length=7)