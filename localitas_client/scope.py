"""Server-side auth/scope helpers — mirrors the Go client's scope.go + auth.go.

For apps that expose their own HTTP endpoints (the external-app model): parse
the platform bearer token (base64-encoded JSON) and enforce a minimum scope.

The scope hierarchy is: guest < read < write < admin.
"""

import base64
import json

SCOPE_GUEST = ""
SCOPE_READ = "read"
SCOPE_WRITE = "write"
SCOPE_ADMIN = "admin"

_RANK = {SCOPE_ADMIN: 3, SCOPE_WRITE: 2, SCOPE_READ: 1, SCOPE_GUEST: 0}


def scope_rank(scope: str) -> int:
    return _RANK.get(scope or "", 0)


def has_scope(user_scope: str, required: str) -> bool:
    """True if ``user_scope`` meets or exceeds ``required``."""
    return scope_rank(user_scope) >= scope_rank(required)


def parse_bearer_token(token: str) -> dict:
    """Decode a Localitas bearer token (base64 JSON) into its identity claims:
    ``user_id``, ``email``, ``name``, ``permission``. Client-side parse only —
    does not validate against the server. Raises ValueError on malformed input."""
    try:
        decoded = base64.standard_b64decode(token)
    except Exception:
        # Tolerate un-padded base64.
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    claims = json.loads(decoded)
    if not isinstance(claims, dict):
        raise ValueError("token payload is not an object")
    return claims


def token_from_auth_header(authorization: str) -> str:
    """Extract the bearer token from an Authorization header value. Returns
    empty string if missing or malformed."""
    if not authorization or not authorization.startswith("Bearer "):
        return ""
    return authorization[len("Bearer "):]


def identity_from_auth_header(authorization: str) -> dict:
    """Parse the caller's identity claims from an Authorization header, or an
    empty dict if absent/invalid."""
    token = token_from_auth_header(authorization)
    if not token:
        return {}
    try:
        return parse_bearer_token(token)
    except Exception:
        return {}


def require_scope(authorization: str, required: str) -> dict:
    """Enforce a minimum scope for a request's Authorization header. Returns the
    caller's identity claims on success. Raises PermissionError if the token is
    missing/invalid (401-equivalent) or under-scoped (403-equivalent).

    Framework-agnostic — call it from a FastAPI dependency, Flask before_request,
    or a WSGI wrapper, passing ``request.headers.get("authorization")``.
    """
    claims = identity_from_auth_header(authorization)
    if not claims.get("user_id") and not claims.get("email"):
        raise PermissionError("authorization required")
    if not has_scope(claims.get("permission", ""), required):
        raise PermissionError("insufficient permission")
    return claims
