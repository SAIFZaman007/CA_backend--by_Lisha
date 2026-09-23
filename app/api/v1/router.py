"""
Mounts every v1 route under a single router.

This file is also where "free" and "paid" are decided. Each coaching router is
mounted behind `require_feature(...)`, which answers 402 for an account with no
live subscription — every verb, every path in that router, including the ones
added next month. Anything mounted without a guard is free on purpose:
marketing pages, the calculators, the account itself and, above all, billing —
locking the page someone buys a plan on is how a paywall traps its own
customers.
"""

from fastapi import APIRouter, Depends

from app.api.v1.endpoints import (
    admin,
    auth,
    billing,
    calculators,
    dashboard,
    exercises,
    gallery,
    messages,
    nutrition,
    progress,
    public,
    tutorials,
    users,
    wellness,
    workouts,
)
from app.core.deps import require_feature

api_router = APIRouter()

# Open to visitors
api_router.include_router(public.router)
api_router.include_router(calculators.router)
api_router.include_router(gallery.router)

# Account
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(billing.router)

# Client portal — coaching. Paid, and enforced here for every method.
PAID_AREAS = (
    (dashboard.router, "dashboard"),
    (workouts.router, "workouts"),
    (exercises.router, "exercise_videos"),
    (nutrition.router, "meal_plan"),
    (progress.router, "progress_tracking"),
    (wellness.router, "sleep_cardio"),
    (tutorials.router, "tutorials"),
    (messages.router, "messaging"),
)

for router, feature in PAID_AREAS:
    api_router.include_router(router, dependencies=[Depends(require_feature(feature))])

# Coach / admin dashboard — role-guarded inside the package.
api_router.include_router(admin.router)