import asyncio
from dataclasses import dataclass
from functools import lru_cache
from hmac import compare_digest

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import (
    InvalidTokenError,
    PyJWKClient,
    PyJWKClientConnectionError,
    PyJWKClientError,
)

from production_inference.config import (
    APP_ENV,
    AUTH_AUDIENCE,
    AUTH_DEV_MODE,
    AUTH_DEV_ROLE,
    AUTH_DEV_TENANT_ID,
    AUTH_DEV_TOKEN,
    AUTH_ISSUER,
    AUTH_JWKS_URL,
)
from production_inference.qdrant import ROLE_CLEARANCE

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    tenant_id: str
    user_role: str
    subject: str


@lru_cache(maxsize=1)
def get_jwks_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url)


def principal_from_claims(claims: dict[str, object]) -> AuthenticatedPrincipal:
    tenant_id = claims.get("tenant_id")
    user_role = claims.get("role")
    subject = claims.get("sub")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HTTPException(status_code=403, detail="Token has no valid tenant claim")
    if not isinstance(user_role, str) or user_role not in ROLE_CLEARANCE:
        raise HTTPException(status_code=403, detail="Token has no authorized role")
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(status_code=403, detail="Token has no valid subject claim")
    return AuthenticatedPrincipal(
        tenant_id=tenant_id.strip(),
        user_role=user_role,
        subject=subject.strip(),
    )


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedPrincipal:
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if APP_ENV == "development" and AUTH_DEV_MODE:
        if not AUTH_DEV_TOKEN:
            raise HTTPException(
                status_code=503,
                detail="Development bearer token is not configured",
            )
        if not compare_digest(credentials.credentials, AUTH_DEV_TOKEN):
            raise HTTPException(
                status_code=401,
                detail="Invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return principal_from_claims(
            {
                "tenant_id": AUTH_DEV_TENANT_ID,
                "role": AUTH_DEV_ROLE,
                "sub": "local-development-user",
            }
        )

    if not all((AUTH_JWKS_URL, AUTH_ISSUER, AUTH_AUDIENCE)):
        raise HTTPException(status_code=503, detail="Identity provider is not configured")

    try:
        jwks_client = get_jwks_client(AUTH_JWKS_URL)
        signing_key = await asyncio.to_thread(
            jwks_client.get_signing_key_from_jwt, credentials.credentials
        )
        claims = jwt.decode(
            credentials.credentials,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=AUTH_AUDIENCE,
            issuer=AUTH_ISSUER,
            options={"require": ["exp", "sub"]},
        )
    except PyJWKClientConnectionError as error:
        raise HTTPException(status_code=503, detail="Identity provider is unavailable") from error
    except (InvalidTokenError, PyJWKClientError) as error:
        raise HTTPException(
            status_code=401,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error

    return principal_from_claims(claims)