"""Workout plan, session and set-logging payloads."""

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import SessionStatus, TrainingLevel
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
    days: list[WorkoutDayOut]


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
