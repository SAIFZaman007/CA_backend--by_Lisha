"""Nutrition: the weekly meal plan and what the client actually ate."""

import uuid
from datetime import date, time

from sqlalchemy import (
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin, UUIDMixin
from app.models.enums import Goal


class MealPlan(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "meal_plans"

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    assigned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    phase: Mapped[Goal] = mapped_column(Enum(Goal, name="goal"), default=Goal.CUT, nullable=False)
    calorie_target: Mapped[int] = mapped_column(Integer, nullable=False)
    protein_target_g: Mapped[int] = mapped_column(Integer, nullable=False)
    carb_target_g: Mapped[int] = mapped_column(Integer, nullable=False)
    fat_target_g: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    meals: Mapped[list["Meal"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="Meal.day_of_week, Meal.order_index",
        lazy="selectin",
    )


class Meal(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "meals"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("meal_plans.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 = Monday
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    serve_time: Mapped[time | None] = mapped_column(Time)
    icon: Mapped[str | None] = mapped_column(String(16))
    calories: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    protein_g: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    carbs_g: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fat_g: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    plan: Mapped[MealPlan] = relationship(back_populates="meals")
    items: Mapped[list["MealItem"]] = relationship(
        back_populates="meal",
        cascade="all, delete-orphan",
        order_by="MealItem.order_index",
        lazy="selectin",
    )


class MealItem(UUIDMixin, Base):
    __tablename__ = "meal_items"

    meal_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("meals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    label: Mapped[str] = mapped_column(String(160), nullable=False)  # "1 cup oats"
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    meal: Mapped[Meal] = relationship(back_populates="items")


class MealLog(UUIDMixin, TimestampMixin, Base):
    """Tick-box adherence tracking, one row per meal per day."""

    __tablename__ = "meal_logs"
    __table_args__ = (UniqueConstraint("client_id", "meal_id", "log_date"),)

    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    meal_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("meals.id", ondelete="CASCADE"), nullable=False
    )
    log_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    actual_calories: Mapped[int | None] = mapped_column(Integer)
