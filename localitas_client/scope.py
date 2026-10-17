"""Server-side auth/scope helpers — mirrors the Go client's scope.go + auth.go.

For apps that expose their own HTTP endpoints (the external-app model): verify
the identity token core puts on every request it forwards to the app, and
enforce a minimum scope.

The scope hierarchy is: guest < read < write < admin.
"""

import base64
import hashlib
import hmac
import json
import os

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


_IDENTITY_KEY_DOMAIN = "localitas-internal-token-v1"
_identity_key = None


def set_identity_key_from_api_token(api_token: str) -> None:
    """Verify identity tokens with the key core derives from its API token.
    Apps normally need not call this: the key is derived from
    LOCALITAS_API_TOKEN, or the token in ~/.localitas/config-core.yaml."""
    global _identity_key
    _identity_key = _derive_identity_key(api_token)


def _derive_identity_key(api_token: str) -> bytes:
    if not api_token:
        return b""
    digest = hashlib.sha256(f"{_IDENTITY_KEY_DOMAIN}:{api_token}".encode()).digest()
    return base64.standard_b64encode(digest)


def _current_identity_key() -> bytes:
    global _identity_key
    if _identity_key is None:
        from .client import default_token

        _identity_key = _derive_identity_key(os.environ.get("LOCALITAS_API_TOKEN") or default_token())
    return _identity_key


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parse_bearer_token(token: str) -> dict:
    """Verify an identity token core put on a forwarded request
    ("payload.signature": base64url JSON claims and their HMAC-SHA256 under
    the key core derives from its API token) and return its claims:
    ``user_id``, ``email``, ``name``, ``permission``. Raises ValueError for an
    unsigned, tampered or wrong-key token, so a caller cannot claim to be
    another user."""
    key = _current_identity_key()
    if not key:
        raise ValueError("no identity key: set LOCALITAS_API_TOKEN")
    payload, sep, sig = token.partition(".")
    if not sep or not payload or not sig:
        raise ValueError("identity token is not signed")
    expected = base64.urlsafe_b64encode(hmac.new(key, payload.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("identity token signature does not verify")
    claims = json.loads(_b64url_decode(payload))
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
