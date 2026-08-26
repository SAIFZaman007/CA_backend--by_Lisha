"""Admin dashboard API.

Every route in this package sits behind `CurrentCoach` (coach or admin) at
minimum; the destructive ones additionally require `CurrentAdmin`. Nothing here
is reachable from a client account.
"""

from fastapi import APIRouter

from app.api.v1.endpoints.admin import (
    catalog,
    catalog_ops,
    clients,
    gallery,
    inbox,
    overview,
    programming,
)

router = APIRouter(prefix="/admin", tags=["admin"])

router.include_router(overview.router)
router.include_router(clients.router)
router.include_router(programming.router)
router.include_router(catalog.router)
# Bulk operations over the exercise library — catalogue import and link
# verification. Separate from `catalog.router`, which is per-row CRUD.
router.include_router(catalog_ops.router)
router.include_router(gallery.router)
router.include_router(inbox.router)

__all__ = ["router"]