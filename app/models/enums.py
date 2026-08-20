"""Domain enums. Stored as native PostgreSQL enums for integrity."""

from enum import StrEnum


class UserRole(StrEnum):
    CLIENT = "client"
    COACH = "coach"
    ADMIN = "admin"


class Sex(StrEnum):
    FEMALE = "female"
    MALE = "male"


class TrainingLevel(StrEnum):
    """Beginner 3 days, Intermediate 4 days, Advanced 5–6 days."""

    LEVEL_1 = "level_1"
    LEVEL_2 = "level_2"
    LEVEL_3 = "level_3"


class Goal(StrEnum):
    CUT = "cut"
    MAINTAIN = "maintain"
    BUILD = "build"


class ActivityLevel(StrEnum):
    SEDENTARY = "sedentary"
    LIGHT = "light"
    MODERATE = "moderate"
    ACTIVE = "active"
    VERY_ACTIVE = "very_active"


class UnitSystem(StrEnum):
    METRIC = "metric"
    IMPERIAL = "imperial"


class Equipment(StrEnum):
    BARBELL = "barbell"
    DUMBBELL = "dumbbell"
    MACHINE = "machine"
    CABLE = "cable"
    BODYWEIGHT = "bodyweight"
    KETTLEBELL = "kettlebell"
    BAND = "band"
    OTHER = "other"


class SessionStatus(StrEnum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class CardioType(StrEnum):
    WALKING = "walking"
    RUNNING = "running"
    CYCLING = "cycling"
    ROWING = "rowing"
    ELLIPTICAL = "elliptical"
    STAIR_CLIMBER = "stair_climber"
    SWIMMING = "swimming"
    HIIT = "hiit"
    SPORTS = "sports"
    OTHER = "other"


class Intensity(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class DataSource(StrEnum):
    MANUAL = "manual"
    WATCH = "watch"


class PhotoPose(StrEnum):
    FRONT = "front"
    SIDE = "side"
    BACK = "back"


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    CONVERTED = "converted"
    CLOSED = "closed"


class BookingStatus(StrEnum):
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TutorialCategory(StrEnum):
    """How the tutorial library is shelved for clients."""

    GETTING_STARTED = "getting_started"
    FORM_TECHNIQUE = "form_technique"
    WARM_UP = "warm_up"
    MOBILITY = "mobility"
    CARDIO = "cardio"
    NUTRITION = "nutrition"
    EQUIPMENT = "equipment"
    RECOVERY = "recovery"


class VideoProvider(StrEnum):
    """Where the recording is hosted. Drives how the player is embedded."""

    YOUTUBE = "youtube"
    VIMEO = "vimeo"
    DIRECT = "direct"