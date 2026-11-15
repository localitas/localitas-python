"""Server-side auth/scope helpers — mirrors the Go client's scope.go + auth.go.

For apps that expose their own HTTP endpoints (the external-app model): verify
the identity token core puts on every request it forwards to the app, and
enforce a minimum scope.

The scope hierarchy is: guest < read < write < admin.
"""

import base64
import hashlib
import json
import os
import time
import urllib.request

from . import _ed25519

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


_ED25519_KID_PREFIX = "ed25519-"
_CLOCK_SKEW = 120
_KEY_FETCH_RETRY = 30
_REFUSED = "legacy identity tokens are no longer accepted"

_public_keys = None  # kid -> Ed25519 public key; None until resolved
_last_fetch = 0.0
_fetched_key = False


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _decode_key(s: str) -> bytes:
    s = s.strip()
    try:
        raw = _b64url_decode(s.replace("+", "-").replace("/", "_"))
    except ValueError as e:
        raise ValueError("identity public key is not base64") from e
    if len(raw) != 32:
        raise ValueError(f"identity public key must be 32 bytes, got {len(raw)}")
    return raw


def ed25519_key_id(public_key: bytes) -> str:
    """Name an identity public key as core does: a prefix and the base64url
    of the first 9 bytes of its SHA-256."""
    digest = hashlib.sha256(public_key).digest()[:9]
    return _ED25519_KID_PREFIX + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _parse_keys(keys) -> dict:
    out = {}
    for k in keys:
        if k and k.strip():
            raw = _decode_key(k)
            out[ed25519_key_id(raw)] = raw
    return out


def set_identity_public_keys(*keys: str) -> None:
    """Verify identity tokens with these Ed25519 public keys (base64url)
    instead of LOCALITAS_IDENTITY_PUBLIC_KEY or the key core serves."""
    global _public_keys
    _public_keys = _parse_keys(keys)


def reset_identity_public_keys() -> None:
    """Resolve the public keys again from LOCALITAS_IDENTITY_PUBLIC_KEY
    (comma-separated), or else from core's GET /api/auth/identity/public-key."""
    global _public_keys, _last_fetch, _fetched_key
    _public_keys, _last_fetch, _fetched_key = None, 0.0, False


def _system_token() -> str:
    from .client import default_token

    return os.environ.get("LOCALITAS_API_TOKEN") or default_token()


def _fetch_public_key() -> None:
    global _public_keys, _last_fetch, _fetched_key
    token = _system_token()
    if not token or time.time() - _last_fetch < _KEY_FETCH_RETRY:
        return
    _last_fetch = time.time()
    base = (os.environ.get("CORE_URL") or "http://localhost:8080").rstrip("/")
    req = urllib.request.Request(base + "/api/auth/identity/public-key", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode())
    except Exception:
        return
    _fetched_key = True
    if body.get("public_key"):
        try:
            _public_keys = _parse_keys([body["public_key"]])
        except ValueError:
            pass


def _resolve() -> dict:
    """The public keys to verify with."""
    global _public_keys
    if _public_keys is None:
        env = os.environ.get("LOCALITAS_IDENTITY_PUBLIC_KEY", "")
        if env:
            try:
                _public_keys = _parse_keys(env.split(","))
            except ValueError:
                pass
        elif not _fetched_key:
            _fetch_public_key()
    return _public_keys or {}


def unverified_claims(token: str) -> dict:
    """An identity token's claims, without verifying it — for a client to
    learn when the token it holds expires."""
    try:
        claims = json.loads(_b64url_decode(token.partition(".")[0]))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def parse_bearer_token(token: str, now: float = None) -> dict:
    """Verify an identity token core put on a forwarded request
    ("payload.signature": base64url JSON claims and core's Ed25519 signature,
    verified with LOCALITAS_IDENTITY_PUBLIC_KEY or the key core serves) and
    return its claims: ``user_id``, ``email``, ``name``, ``permission``,
    ``iat``, ``exp``. Raises ValueError for an unsigned, tampered or wrong-key
    token, and one expired (beyond two minutes of clock skew) or issued in
    the future. Tokens not signed with an Ed25519 key (the old HMAC tokens)
    and tokens without an expiry are always refused."""
    payload, sep, sig = token.partition(".")
    if not sep or not payload or not sig:
        raise ValueError("identity token is not signed")
    try:
        claims = json.loads(_b64url_decode(payload))
    except Exception as e:
        raise ValueError("identity token payload is not JSON") from e
    if not isinstance(claims, dict):
        raise ValueError("token payload is not an object")

    kid = claims.get("kid") or ""
    if not isinstance(kid, str) or not kid.startswith(_ED25519_KID_PREFIX):
        raise ValueError(_REFUSED)
    keys = _resolve()
    pub = keys.get(kid)
    if pub is None:
        raise ValueError("identity token signed with an unknown key" if keys else "no identity public key: set LOCALITAS_IDENTITY_PUBLIC_KEY")
    try:
        raw_sig = _b64url_decode(sig)
    except ValueError as e:
        raise ValueError("identity token signature does not verify") from e
    if not _ed25519.verify(pub, payload.encode(), raw_sig):
        raise ValueError("identity token signature does not verify")

    exp = int(claims.get("exp") or 0)
    if not exp:
        raise ValueError(_REFUSED)
    current = time.time() if now is None else now
    if current > exp + _CLOCK_SKEW:
        raise ValueError("identity token expired")
    if int(claims.get("iat") or 0) > current + _CLOCK_SKEW:
        raise ValueError("identity token issued in the future")
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
