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


class MuscleGroup(StrEnum):
    """The muscle-group taxonomy the coach browses the library by.

    Deliberately an enum rather than the free-text `Exercise.target_muscle`
    that sits alongside it. The coach's picker groups every movement under
    exactly one of these headings, and a typo'd string ("Quadriceps" vs
    "Quads") would silently create a 23rd group that half the library lands in
    and no filter finds. `target_muscle` stays free text for the human-readable
    label ("Upper chest", "Rear delts"); this is the machine-readable bucket.

    Ordering matches the reference chart the client supplied, which is also
    alphabetical apart from `UPPER_BACK` sitting next to `LOWER_BACK`.
    """

    ABDUCTORS = "abductors"
    ABS = "abs"
    ADDUCTORS = "adductors"
    BICEPS = "biceps"
    CALVES = "calves"
    CHEST = "chest"
    FOREARMS = "forearms"
    GLUTES = "glutes"
    HAMSTRINGS = "hamstrings"
    HIP_FLEXORS = "hip_flexors"
    IT_BAND = "it_band"
    LATS = "lats"
    LOWER_BACK = "lower_back"
    UPPER_BACK = "upper_back"
    NECK = "neck"
    OBLIQUES = "obliques"
    PALMAR_FASCIA = "palmar_fascia"
    PLANTAR_FASCIA = "plantar_fascia"
    QUADS = "quads"
    SHOULDERS = "shoulders"
    TRAPS = "traps"
    TRICEPS = "triceps"


# Display labels for the API and both frontends. Kept beside the enum so a new
# group cannot be added without someone deciding what it is called on screen.
MUSCLE_GROUP_LABELS: dict[MuscleGroup, str] = {
    MuscleGroup.ABDUCTORS: "Abductors",
    MuscleGroup.ABS: "Abs",
    MuscleGroup.ADDUCTORS: "Adductors",
    MuscleGroup.BICEPS: "Biceps",
    MuscleGroup.CALVES: "Calves",
    MuscleGroup.CHEST: "Chest",
    MuscleGroup.FOREARMS: "Forearms",
    MuscleGroup.GLUTES: "Glutes",
    MuscleGroup.HAMSTRINGS: "Hamstrings",
    MuscleGroup.HIP_FLEXORS: "Hip Flexors",
    MuscleGroup.IT_BAND: "IT Band",
    MuscleGroup.LATS: "Lats",
    MuscleGroup.LOWER_BACK: "Lower Back",
    MuscleGroup.UPPER_BACK: "Upper Back",
    MuscleGroup.NECK: "Neck",
    MuscleGroup.OBLIQUES: "Obliques",
    MuscleGroup.PALMAR_FASCIA: "Palmar Fascia",
    MuscleGroup.PLANTAR_FASCIA: "Plantar Fascia",
    MuscleGroup.QUADS: "Quads",
    MuscleGroup.SHOULDERS: "Shoulders",
    MuscleGroup.TRAPS: "Traps",
    MuscleGroup.TRICEPS: "Triceps",
}


class Equipment(StrEnum):
    """What the movement needs.

    The first eight values are the original set and must keep their exact
    spelling — rows in `exercises` and `video_tutorials` already hold them, and
    a native PostgreSQL enum cannot rename a value without rewriting every
    dependent column. Everything below `OTHER` is additive.
    """

    BARBELL = "barbell"
    DUMBBELL = "dumbbell"
    MACHINE = "machine"
    CABLE = "cable"
    BODYWEIGHT = "bodyweight"
    KETTLEBELL = "kettlebell"
    BAND = "band"
    OTHER = "other"

    # Added alongside the full exercise catalogue.
    EZ_BAR = "ez_bar"
    SMITH_MACHINE = "smith_machine"
    MEDICINE_BALL = "medicine_ball"
    WEIGHT_PLATE = "weight_plate"
    TRAP_BAR = "trap_bar"
    SUSPENSION = "suspension"
    STRETCH = "stretch"
    FOAM_ROLLER = "foam_roller"
    SLED = "sled"
    CARDIO_MACHINE = "cardio_machine"


EQUIPMENT_LABELS: dict[Equipment, str] = {
    Equipment.BARBELL: "Barbell",
    Equipment.DUMBBELL: "Dumbbell",
    Equipment.MACHINE: "Machine",
    Equipment.CABLE: "Cable",
    Equipment.BODYWEIGHT: "Bodyweight",
    Equipment.KETTLEBELL: "Kettlebell",
    Equipment.BAND: "Resistance Band",
    Equipment.OTHER: "Other",
    Equipment.EZ_BAR: "EZ Bar",
    Equipment.SMITH_MACHINE: "Smith Machine",
    Equipment.MEDICINE_BALL: "Medicine Ball",
    Equipment.WEIGHT_PLATE: "Weight Plate",
    Equipment.TRAP_BAR: "Trap Bar",
    Equipment.SUSPENSION: "Suspension Trainer",
    Equipment.STRETCH: "Stretch",
    Equipment.FOAM_ROLLER: "Foam Roller",
    Equipment.SLED: "Sled",
    Equipment.CARDIO_MACHINE: "Cardio Machine",
}


class Mechanics(StrEnum):
    """Whether the movement crosses one joint or several.

    Used by the coach's picker to answer "give me a compound to open the day",
    which is a different question from "give me something for biceps".
    """

    COMPOUND = "compound"
    ISOLATION = "isolation"
    STATIC = "static"


class ForceType(StrEnum):
    PUSH = "push"
    PULL = "pull"
    STATIC = "static"
    HINGE = "hinge"
    SQUAT = "squat"
    CARRY = "carry"


class GalleryCategory(StrEnum):
    """How the Hall of the Coach is shelved."""

    TRANSFORMATIONS = "transformations"
    COACHING = "coaching"
    COMPETITION = "competition"
    GYM = "gym"
    CERTIFICATIONS = "certifications"
    COMMUNITY = "community"
    BEHIND_THE_SCENES = "behind_the_scenes"


GALLERY_CATEGORY_LABELS: dict[GalleryCategory, str] = {
    GalleryCategory.TRANSFORMATIONS: "Transformations",
    GalleryCategory.COACHING: "Coaching",
    GalleryCategory.COMPETITION: "Competition",
    GalleryCategory.GYM: "In the Gym",
    GalleryCategory.CERTIFICATIONS: "Certifications",
    GalleryCategory.COMMUNITY: "Community",
    GalleryCategory.BEHIND_THE_SCENES: "Behind the Scenes",
}


class AttachmentKind(StrEnum):
    """What a message carries alongside its text.

    Only `IMAGE` is accepted today — a client photographing their setup, a
    plate loaded wrong, a meal, a scale reading. The enum exists so adding
    documents later is a value, not a schema change to every attachment row.
    """

    IMAGE = "image"


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


class SubscriptionStatus(StrEnum):
    """Mirrors Stripe's subscription statuses.

    Kept as Stripe's own vocabulary rather than a simplified one, so a webhook
    can be written straight to the column without a lossy translation step in
    between — the place such bugs like to hide.
    """

    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    UNPAID = "unpaid"
    PAUSED = "paused"


# The statuses that actually entitle someone to their coaching tier.
# `past_due` is deliberately included: a failed card should not lock a paying
# client out of their programme mid-week while Stripe retries. Stripe moves the
# subscription to `unpaid` or `canceled` when retries are exhausted, and that is
# the point where access stops.
ENTITLING_STATUSES = frozenset(
    {SubscriptionStatus.TRIALING, SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE}
)


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"