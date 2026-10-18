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


_IDENTITY_KEY_DOMAIN = "localitas-internal-token-v1"
_ED25519_KID_PREFIX = "ed25519-"
_CLOCK_SKEW = 120
_KEY_FETCH_RETRY = 30

_identity_key = None  # legacy HMAC key
_public_keys = None  # kid -> Ed25519 public key; None until resolved
_last_fetch = 0.0
_fetched_key = False
_legacy_explicit = None
_legacy_fetched = None


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
    global _public_keys, _legacy_fetched
    _public_keys, _legacy_fetched = _parse_keys(keys), None


def reset_identity_public_keys() -> None:
    """Resolve the public keys again from LOCALITAS_IDENTITY_PUBLIC_KEY
    (comma-separated), or else from core's GET /api/auth/identity/public-key."""
    global _public_keys, _last_fetch, _fetched_key, _legacy_fetched
    _public_keys, _last_fetch, _fetched_key, _legacy_fetched = None, 0.0, False, None


def set_accept_legacy_identity_tokens(accept: bool) -> None:
    """Accept (or refuse) HMAC-signed tokens and tokens without an expiry,
    instead of LOCALITAS_ACCEPT_LEGACY_IDENTITY_TOKENS or what core reports."""
    global _legacy_explicit
    _legacy_explicit = bool(accept)


def reset_accept_legacy_identity_tokens() -> None:
    global _legacy_explicit
    _legacy_explicit = None


def set_identity_key_from_api_token(api_token: str) -> None:
    """Verify legacy identity tokens with the key core derives from its API
    token. Apps normally need not call this: the key is derived from
    LOCALITAS_API_TOKEN, or the token in ~/.localitas/config-core.yaml."""
    global _identity_key
    _identity_key = _derive_identity_key(api_token)


def _derive_identity_key(api_token: str) -> bytes:
    if not api_token:
        return b""
    digest = hashlib.sha256(f"{_IDENTITY_KEY_DOMAIN}:{api_token}".encode()).digest()
    return base64.standard_b64encode(digest)


def _system_token() -> str:
    from .client import default_token

    return os.environ.get("LOCALITAS_API_TOKEN") or default_token()


def _current_identity_key() -> bytes:
    global _identity_key
    if _identity_key is None:
        _identity_key = _derive_identity_key(_system_token())
    return _identity_key


def _fetch_public_key() -> None:
    global _public_keys, _last_fetch, _fetched_key, _legacy_fetched
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
    _legacy_fetched = bool(body.get("accept_legacy"))
    if body.get("public_key"):
        try:
            _public_keys = _parse_keys([body["public_key"]])
        except ValueError:
            pass


def _resolve():
    """The public keys and legacy policy to verify with."""
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
    legacy = True
    env = os.environ.get("LOCALITAS_ACCEPT_LEGACY_IDENTITY_TOKENS", "").lower()
    if _legacy_explicit is not None:
        legacy = _legacy_explicit
    elif env in ("false", "0"):
        legacy = False
    elif env in ("true", "1"):
        legacy = True
    elif _legacy_fetched is not None:
        legacy = _legacy_fetched
    return _public_keys or {}, legacy


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
    the future. Legacy tokens — HMAC-signed under the key derived from the API
    token, or without an expiry — verify only while legacy tokens are
    accepted."""
    payload, sep, sig = token.partition(".")
    if not sep or not payload or not sig:
        raise ValueError("identity token is not signed")
    try:
        claims = json.loads(_b64url_decode(payload))
    except Exception as e:
        raise ValueError("identity token payload is not JSON") from e
    if not isinstance(claims, dict):
        raise ValueError("token payload is not an object")

    keys, legacy = _resolve()
    kid = claims.get("kid") or ""
    if kid.startswith(_ED25519_KID_PREFIX):
        pub = keys.get(kid)
        if pub is None:
            raise ValueError("identity token signed with an unknown key" if keys else "no identity public key: set LOCALITAS_IDENTITY_PUBLIC_KEY")
        try:
            raw_sig = _b64url_decode(sig)
        except ValueError as e:
            raise ValueError("identity token signature does not verify") from e
        if not _ed25519.verify(pub, payload.encode(), raw_sig):
            raise ValueError("identity token signature does not verify")
    else:
        if not legacy:
            raise ValueError("legacy identity tokens are no longer accepted")
        key = _current_identity_key()
        if not key:
            raise ValueError("no identity key: set LOCALITAS_API_TOKEN")
        expected = base64.urlsafe_b64encode(hmac.new(key, payload.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("identity token signature does not verify")

    exp = int(claims.get("exp") or 0)
    if not exp:
        if not legacy:
            raise ValueError("legacy identity tokens are no longer accepted")
        return claims
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
