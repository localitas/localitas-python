"""Identity tokens: core's Ed25519 signature, expiry, the legacy policy, and
clients refreshing the token they act with."""

import base64
import calendar
import hashlib
import hmac
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from contextlib import contextmanager

from localitas_client import LocalitasClient, scope
from localitas_client import _ed25519
from localitas_client import client as client_module

# What core signs at T0 under the Ed25519 seed of 32 bytes of 7; core's
# internal/auth golden test pins the same strings.
GOLDEN_PUBLIC_KEY = "6kpsY-KcUgq-9VB7Ey7F-ZVHdq6-vnuSQh7qaRRG0iw"
GOLDEN_TOKEN = "eyJlbWFpbCI6ImFsaWNlQHRlc3QubG9jYWwiLCJuYW1lIjoiQWxpY2UiLCJ1c2VyX2lkIjoidTEiLCJwZXJtaXNzaW9uIjoid3JpdGUiLCJpYXQiOjE3OTIyMzg0MDAsImV4cCI6MTc5MjIzODcwMCwibWF4IjoxNzkyMzI0ODAwLCJraWQiOiJlZDI1NTE5LV9vRXNFdk9yVE9hcyJ9.C8hu4KQIKHVrA_NyuOnESy33xUYWVqZWDyrONgSpvaTPxCU_aa7xf1N9POyND7eOnwGTh0Pffo71Y-mB3yO9Dg"
LEGACY_GOLDEN_TOKEN = "eyJlbWFpbCI6ImFsaWNlQHRlc3QubG9jYWwiLCJuYW1lIjoiQWxpY2UiLCJ1c2VyX2lkIjoidTEiLCJwZXJtaXNzaW9uIjoid3JpdGUifQ.qpBZu3EVKLK3lKw_8Y2WD_VP9nCqx849D30L0r9fo5Y"
TEST_API_TOKEN = "lt_test_cluster_token"
T0 = calendar.timegm((2026, 10, 17, 12, 0, 0))
SEED = bytes([7]) * 32


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_ed25519(seed, claims):
    pub = _ed25519.public_key(seed)
    claims = dict(claims, kid=scope.ed25519_key_id(pub))
    payload = b64(json.dumps(claims).encode())
    return payload + "." + b64(_ed25519.sign(seed, payload.encode()))


def sign_hmac(api_token, claims):
    payload = b64(json.dumps(claims).encode())
    key = base64.standard_b64encode(hashlib.sha256(f"localitas-internal-token-v1:{api_token}".encode()).digest())
    return payload + "." + b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())


@contextmanager
def environment(**values):
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def clock_at(t):
    saved = client_module._now
    client_module._now = lambda: t
    try:
        yield
    finally:
        client_module._now = saved


def fresh(now, ttl=300):
    return {"user_id": "u1", "email": "alice@test.local", "permission": "write", "iat": now, "exp": now + ttl, "max": now + 86400}


class IdentityTestCase(unittest.TestCase):
    def setUp(self):
        scope.set_identity_key_from_api_token(TEST_API_TOKEN)
        scope.set_identity_public_keys(GOLDEN_PUBLIC_KEY)
        scope.reset_accept_legacy_identity_tokens()

    def tearDown(self):
        scope.set_identity_public_keys(GOLDEN_PUBLIC_KEY)
        scope.reset_accept_legacy_identity_tokens()


class TestEd25519(unittest.TestCase):
    def test_rfc8032_vector(self):
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        self.assertEqual(_ed25519.public_key(seed).hex(), "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = _ed25519.sign(seed, b"")
        self.assertTrue(_ed25519._verify_pure(_ed25519.public_key(seed), b"", sig))
        self.assertFalse(_ed25519._verify_pure(_ed25519.public_key(seed), b"x", sig))


class TestIdentityTokens(IdentityTestCase):
    def test_verifies_core_ed25519_token(self):
        claims = scope.parse_bearer_token(GOLDEN_TOKEN, now=T0)
        self.assertEqual((claims["user_id"], claims["permission"], claims["exp"]), ("u1", "write", T0 + 300))

    def test_rejects_expired_and_future_tokens(self):
        scope.parse_bearer_token(GOLDEN_TOKEN, now=T0 + 300 + 119)
        with self.assertRaisesRegex(ValueError, "expired"):
            scope.parse_bearer_token(GOLDEN_TOKEN, now=T0 + 300 + 121)
        with self.assertRaisesRegex(ValueError, "future"):
            scope.parse_bearer_token(GOLDEN_TOKEN, now=T0 - 121)

    def test_rejects_another_clusters_key(self):
        with self.assertRaises(ValueError):
            scope.parse_bearer_token(sign_ed25519(bytes(32), fresh(T0)), now=T0)

    def test_legacy_tokens_only_while_accepted(self):
        with_exp = sign_hmac(TEST_API_TOKEN, fresh(T0))
        scope.parse_bearer_token(LEGACY_GOLDEN_TOKEN, now=T0)
        scope.parse_bearer_token(with_exp, now=T0)
        scope.set_accept_legacy_identity_tokens(False)
        for token in (LEGACY_GOLDEN_TOKEN, with_exp):
            with self.assertRaisesRegex(ValueError, "legacy"):
                scope.parse_bearer_token(token, now=T0)
            self.assertEqual(scope.identity_from_auth_header(f"Bearer {token}"), {})
        scope.parse_bearer_token(GOLDEN_TOKEN, now=T0)

    def test_public_key_and_policy_from_environment(self):
        with environment(LOCALITAS_IDENTITY_PUBLIC_KEY=GOLDEN_PUBLIC_KEY, LOCALITAS_ACCEPT_LEGACY_IDENTITY_TOKENS="false"):
            scope.reset_identity_public_keys()
            scope.reset_accept_legacy_identity_tokens()
            scope.parse_bearer_token(GOLDEN_TOKEN, now=T0)
            with self.assertRaisesRegex(ValueError, "legacy"):
                scope.parse_bearer_token(LEGACY_GOLDEN_TOKEN, now=T0)


class _Core(BaseHTTPRequestHandler):
    """Stands in for core: serves the identity public key and refreshes."""
    seen = []
    asked_key_with = []
    refreshes = []
    refreshed_token = ""

    def log_message(self, *args):
        pass

    def _send(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/auth/identity/public-key":
            _Core.asked_key_with.append(self.headers.get("Authorization"))
            self._send({"alg": "EdDSA", "public_key": GOLDEN_PUBLIC_KEY, "accept_legacy": False})
            return
        _Core.seen.append(self.headers.get("Authorization"))
        self._send([])

    def do_POST(self):
        if self.path == "/api/auth/identity/refresh":
            _Core.refreshes.append(self.headers.get("Authorization"))
            self._send({"token": _Core.refreshed_token})
            return
        self._send({})


class TestWithCore(IdentityTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Core)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        super().setUp()
        _Core.seen, _Core.asked_key_with, _Core.refreshes = [], [], []

    def test_fetches_the_public_key_from_core(self):
        with environment(CORE_URL=self.url, LOCALITAS_API_TOKEN=TEST_API_TOKEN, LOCALITAS_IDENTITY_PUBLIC_KEY="", LOCALITAS_ACCEPT_LEGACY_IDENTITY_TOKENS=""):
            scope.reset_identity_public_keys()
            scope.parse_bearer_token(GOLDEN_TOKEN, now=T0)
            self.assertEqual(_Core.asked_key_with, [f"Bearer {TEST_API_TOKEN}"])
            with self.assertRaisesRegex(ValueError, "legacy"):
                scope.parse_bearer_token(LEGACY_GOLDEN_TOKEN, now=T0)

    def test_client_refreshes_an_identity_token_near_expiry(self):
        original = sign_ed25519(SEED, fresh(T0))
        _Core.refreshed_token = sign_ed25519(SEED, fresh(T0 + 270))
        client = LocalitasClient(self.url, original)
        with clock_at(T0):
            client.list_databases()
        with clock_at(T0 + 270):
            client.list_databases()
        self.assertEqual(_Core.refreshes, [f"Bearer {original}"])
        self.assertEqual(_Core.seen, [f"Bearer {original}", f"Bearer {_Core.refreshed_token}"])


if __name__ == "__main__":
    unittest.main()
