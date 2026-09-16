"""Fail-closed authorization for production model-governance writes."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.config.settings import settings


def require_promotion_governance_principal(
    governance_token: str | None = Header(
        default=None,
        alias="X-Claw-Governance-Token",
    ),
) -> str:
    """Authenticate the single configured governance principal.

    Confirmation phrases remain protection against operator mistakes; this token is
    the actual authorization boundary.  When no token is configured, production
    mutations are deliberately unavailable.
    """

    configured = str(settings.PROMOTION_GOVERNANCE_TOKEN or "").strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "model governance writes are disabled: configure "
                "PROMOTION_GOVERNANCE_TOKEN on the backend"
            ),
        )
    supplied = str(governance_token or "")
    if not supplied or not secrets.compare_digest(supplied, configured):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid model governance token",
        )
    principal = str(settings.PROMOTION_GOVERNANCE_OPERATOR or "").strip()
    return principal[:80] or "governance_admin"
