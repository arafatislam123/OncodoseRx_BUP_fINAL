"""Operator token check for anything that changes state or injects chaos."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from app.config import settings


def require_operator(authorization: str = Header(default="")) -> str:
    token = settings.operator_token
    if not token:
        raise HTTPException(503, "OPERATOR_TOKEN is not configured on the server")
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(401, "a valid operator token is required", headers={"WWW-Authenticate": "Bearer"})
    return "operator"
