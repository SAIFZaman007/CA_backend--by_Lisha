"""Mounts every v1 route under a single router."""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    auth,
    calculators,
    dashboard,
    exercises,
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

# Account
api_router.include_router(auth.router)
api_router.include_router(users.router)

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