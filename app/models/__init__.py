"""Import every model here so Alembic autogenerate and SQLAlchemy see them all."""

from app.core.database import Base
from app.models.catalog import Exercise, Program, Testimonial
from app.models.engagement import ConsultationBooking, Lead, Message, MessageThread
from app.models.enums import (
    ActivityLevel,
    BookingStatus,
    CardioType,
    DataSource,
    Equipment,
    Goal,
    Intensity,
    LeadStatus,
    PhotoPose,
    SessionStatus,
    Sex,
    TrainingLevel,
    TutorialCategory,
    UnitSystem,
    UserRole,
    VideoProvider,
)
from app.models.media import VideoTutorial
from app.models.nutrition import Meal, MealItem, MealLog, MealPlan
from app.models.tracking import (
    BodyMeasurement,
    CardioLog,
    ProgressPhoto,
    SleepLog,
    WeightLog,
)
from app.models.training import SetLog, WorkoutDay, WorkoutDayExercise, WorkoutPlan, WorkoutSession
from app.models.user import ClientProfile, RefreshSession, User

__all__ = [
    "ActivityLevel",
    "Base",
    "BodyMeasurement",
    "BookingStatus",
    "CardioLog",
    "CardioType",
    "ClientProfile",
    "ConsultationBooking",
    "DataSource",
    "Equipment",
    "Exercise",
    "Goal",
    "Intensity",
    "Lead",
    "LeadStatus",
    "Meal",
    "MealItem",
    "MealLog",
    "MealPlan",
    "Message",
    "MessageThread",
    "PhotoPose",
    "Program",
    "ProgressPhoto",
    "RefreshSession",
    "SessionStatus",
    "SetLog",
    "Sex",
    "SleepLog",
    "Testimonial",
    "TrainingLevel",
    "TutorialCategory",
    "UnitSystem",
    "User",
    "UserRole",
    "VideoProvider",
    "VideoTutorial",
    "WeightLog",
    "WorkoutDay",
    "WorkoutDayExercise",
    "WorkoutPlan",
    "WorkoutSession",
]