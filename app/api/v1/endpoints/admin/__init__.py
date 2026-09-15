"""
Admin dashboard API.

Every route in this package sits behind `CurrentCoach` (coach or admin) at
minimum; the destructive ones additionally require `CurrentAdmin`. Nothing here
is reachable from a client account.
"""

from fastapi import APIRouter

from app.api.v1.endpoints.admin import (
    billing,
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
router.include_router(catalog_ops.router)
router.include_router(gallery.router)
router.include_router(inbox.router)
router.include_router(billing.router)

__all__ = ["router"]