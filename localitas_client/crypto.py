"""Client-side AES-256-GCM encryption — mirrors the Go client's crypto.go.

Produces/consumes the platform's ``enc:``-prefixed format so values encrypted
here interoperate with Go components. The 32-byte key lives at
``~/.localitas/secret.key`` and is auto-generated on first use.

Requires the ``cryptography`` package, imported lazily so it is only needed by
callers that actually encrypt/decrypt.
"""

import base64
import os
from pathlib import Path

_key = None


def _get_or_create_key():
    global _key
    if _key is not None:
        return _key
    key_path = Path.home() / ".localitas" / "secret.key"
    if key_path.exists():
        data = key_path.read_bytes()
        if len(data) == 32:
            _key = data
            return _key
    key = os.urandom(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # 0o600: owner read/write only.
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    _key = key
    return _key


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "localitas_client.crypto requires the 'cryptography' package "
            "(pip install cryptography)"
        ) from exc
    return AESGCM(_get_or_create_key())


def encrypt(plaintext: str) -> str:
    """Encrypt a string, returning ``enc:`` + base64(nonce || ciphertext).
    Empty input returns empty string."""
    if plaintext == "":
        return ""
    nonce = os.urandom(12)
    ct = _aesgcm().encrypt(nonce, plaintext.encode(), None)
    return "enc:" + base64.standard_b64encode(nonce + ct).decode()


def decrypt(encoded: str) -> str:
    """Decrypt a value produced by :func:`encrypt`. Values not prefixed with
    ``enc:`` are returned unchanged (plaintext passthrough)."""
    if encoded == "":
        return ""
    if not encoded.startswith("enc:"):
        return encoded
    raw = base64.standard_b64decode(encoded[4:])
    if len(raw) < 12:
        raise ValueError("ciphertext too short")
    nonce, ct = raw[:12], raw[12:]
    return _aesgcm().decrypt(nonce, ct, None).decode()
