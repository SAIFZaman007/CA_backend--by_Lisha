"""Mounts every v1 route under a single router."""

from fastapi import APIRouter

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

api_router = APIRouter()

# Open to visitors
api_router.include_router(public.router)
api_router.include_router(calculators.router)
# The Hall of the Coach. Public and unauthenticated on purpose — it is
# marketing imagery meant to be crawled and indexed, which is the opposite of
# how `progress.router` treats a check-in photo.
api_router.include_router(gallery.router)

# Account
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(billing.router)

# Client portal
api_router.include_router(dashboard.router)
api_router.include_router(workouts.router)
api_router.include_router(exercises.router)
api_router.include_router(nutrition.router)
api_router.include_router(progress.router)
api_router.include_router(wellness.router)
api_router.include_router(tutorials.router)
api_router.include_router(messages.router)

# Coach / admin dashboard — role-guarded inside the package.
api_router.include_router(admin.router)