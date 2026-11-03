"""Regression tests for caller authentication and per-action authorization."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import time
import types
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from agentcore_test_stub import install as install_agentcore_test_stub

PROJECT_DIR = Path(__file__).resolve().parents[1]
os.environ.setdefault("AGENT_ID", "agent-a")
os.environ.setdefault("TEST_RUN_ID", "unit-test")
os.environ.setdefault("DRIVE9_TEST_TOKEN", "unit-test-token")
os.environ.setdefault(
    "AUTHZ_DISCOVERY_URL",
    "https://issuer.example/.well-known/openid-configuration",
)
os.environ.setdefault("AUTHZ_ALLOWED_CLIENTS", "unit-test-client")
install_agentcore_test_stub()


def _load_app():
    loader = importlib.machinery.SourceFileLoader("authorization_test_app", str(PROJECT_DIR / "app.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


app = _load_app()


def caller(*scopes: str):
    return app.AuthorizedCaller(
        subject="test-user",
        caller_id="9f86d081884c7d65",
        scopes=frozenset(scopes),
    )


class AuthorizationMatrixTests(unittest.TestCase):
    def test_every_action_has_an_explicit_required_scope(self) -> None:
        self.assertEqual(set(app.ACTIONS), set(app.ACTION_REQUIRED_SCOPE))
        self.assertEqual(len(app.ACTIONS), 18)

    def test_scope_hierarchy_is_least_privilege(self) -> None:
        read_actions = {"read", "list", "stat", "find", "search"}
        write_actions = {
            "write", "append", "copy", "move", "handoff_prepare", "handoff_continue"
        }
        operator_actions = {"preflight", "delete", "permission_test", "visibility_test"}
        admin_actions = {"mount_test", "git_prepare", "git_verify"}

        expectations = {
            app.AUTHZ_READ_SCOPE: read_actions,
            app.AUTHZ_WRITE_SCOPE: read_actions | write_actions,
            app.AUTHZ_OPERATOR_SCOPE: read_actions | write_actions | operator_actions,
            app.AUTHZ_ADMIN_SCOPE: set(app.ACTIONS),
        }
        for scope, allowed_actions in expectations.items():
            identity = caller(scope)
            for action in app.ACTIONS:
                with self.subTest(scope=scope, action=action):
                    if action in allowed_actions:
                        self.assertEqual(app._authorize_action(identity, action), app.ACTION_REQUIRED_SCOPE[action])
                    else:
                        with self.assertRaises(app.AuthorizationError):
                            app._authorize_action(identity, action)

    def test_read_only_caller_cannot_reach_delete_handler(self) -> None:
        original_authenticate = app._authenticate_request
        original_delete = app.ACTIONS["delete"]
        called = False

        def dangerous_handler(_payload):
            nonlocal called
            called = True
            return {"status": "PASS"}

        app._authenticate_request = lambda _context: caller(app.AUTHZ_READ_SCOPE)
        app.ACTIONS["delete"] = dangerous_handler
        try:
            result = app.handle_request({"action": "delete", "path": "important"})
        finally:
            app._authenticate_request = original_authenticate
            app.ACTIONS["delete"] = original_delete
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(result["error_code"], "ACCESS_DENIED")
        self.assertFalse(called)

    def test_unknown_action_does_not_advertise_the_action_surface(self) -> None:
        original_authenticate = app._authenticate_request
        app._authenticate_request = lambda _context: caller(app.AUTHZ_ADMIN_SCOPE)
        try:
            result = app.handle_request({"action": "does_not_exist"})
        finally:
            app._authenticate_request = original_authenticate
        self.assertEqual(result["error_code"], "UNKNOWN_ACTION")
        self.assertNotIn("available_actions", result)

    def test_missing_trusted_request_context_is_denied(self) -> None:
        result = app.handle_request({"action": "read", "path": "x"}, context=None)
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(result["error_code"], "ACCESS_DENIED")


class JwtAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.original_openid = app._openid_configuration
        self.original_jwk_client = app._jwk_client
        app._openid_configuration = lambda: ("https://issuer.example", "https://issuer.example/jwks")
        app._jwk_client = lambda _uri: types.SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: types.SimpleNamespace(
                key=self.private_key.public_key()
            )
        )

    def tearDown(self) -> None:
        app._openid_configuration = self.original_openid
        app._jwk_client = self.original_jwk_client

    def _token(self, *, key=None, scope: str = "drive9.read") -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": "https://issuer.example",
                "sub": "user-123",
                "client_id": "unit-test-client",
                "scope": scope,
                "iat": now,
                "exp": now + 300,
            },
            key or self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )

    def test_verified_token_provides_server_trusted_scopes(self) -> None:
        context = types.SimpleNamespace(
            request_headers={"authorization": f"Bearer {self._token()}"}
        )
        identity = app._authenticate_request(context)
        self.assertEqual(identity.scopes, frozenset({app.AUTHZ_READ_SCOPE}))
        self.assertEqual(identity.caller_id, "fcdec6df4d44dbc6")

    def test_bad_signature_is_denied(self) -> None:
        attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        context = types.SimpleNamespace(
            request_headers={"Authorization": f"Bearer {self._token(key=attacker_key)}"}
        )
        with self.assertRaises(app.AuthorizationError):
            app._authenticate_request(context)

    def test_jwks_lookup_failure_is_denied(self) -> None:
        app._jwk_client = lambda _uri: types.SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: (_ for _ in ()).throw(
                jwt.PyJWKClientError("unknown signing key")
            )
        )
        context = types.SimpleNamespace(
            request_headers={"Authorization": f"Bearer {self._token()}"}
        )
        with self.assertRaises(app.AuthorizationError):
            app._authenticate_request(context)


class ServerOwnedVerdictTests(unittest.TestCase):
    def _permission(self, agent_id: str, payload: dict, *, write_ok: bool, outside_ok: bool = False) -> dict:
        original = (app._ensure_context, app._write_text, app._run, app.AGENT_ID)
        app._ensure_context = lambda: {"status": "ready"}
        app._write_text = lambda *_args, **_kwargs: {"exit_code": 0 if write_ok else 1}
        app._run = lambda *_args, **_kwargs: {"exit_code": 0 if outside_ok else 1}
        app.AGENT_ID = agent_id
        try:
            return app.action_permission_test(payload)
        finally:
            app._ensure_context, app._write_text, app._run, app.AGENT_ID = original

    def _visibility(self, agent_id: str, payload: dict, *, visible: bool) -> dict:
        original = (app._ensure_context, app._run, app.AGENT_ID, app.SHARED_BASE_REMOTE)
        app._ensure_context = lambda: {"status": "ready"}
        app._run = lambda *_args, **_kwargs: {"exit_code": 0 if visible else 1}
        app.AGENT_ID = agent_id
        app.SHARED_BASE_REMOTE = ":/agentcore-tests/unit-test/shared"
        try:
            return app.action_visibility_test(payload)
        finally:
            app._ensure_context, app._run, app.AGENT_ID, app.SHARED_BASE_REMOTE = original

    def test_permission_verdict_ignores_caller_expectation(self) -> None:
        result = self._permission("agent-c", {"expect_write_allowed": True}, write_ok=False)
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["expected_write_allowed"])

    def test_overprivileged_agent_c_write_fails_even_if_caller_expects_it(self) -> None:
        result = self._permission("agent-c", {"expect_write_allowed": True}, write_ok=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(result["actual_write_allowed"])

    def test_outside_scope_read_fails_permission_test(self) -> None:
        result = self._permission("agent-a", {}, write_ok=True, outside_ok=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["actual_outside_scope_denied"])

    def test_deployment_env_cannot_relax_write_expectation(self) -> None:
        os.environ["EXPECTED_DRIVE9_WRITE_ALLOWED"] = "true"
        try:
            result = self._permission("agent-c", {}, write_ok=True)
        finally:
            del os.environ["EXPECTED_DRIVE9_WRITE_ALLOWED"]
        self.assertEqual(result["status"], "FAIL")

    def test_unknown_agent_fails_closed(self) -> None:
        self.assertEqual(self._permission("agent-unknown", {}, write_ok=True)["status"], "FAIL")
        self.assertEqual(self._visibility("agent-unknown", {}, visible=True)["status"], "FAIL")

    def test_visibility_verdict_uses_deployment_policy(self) -> None:
        result = self._visibility("agent-c", {"path": ":/attacker/chosen", "expect_visible": True}, visible=False)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["target"], ":/agentcore-tests/unit-test/shared")
        self.assertFalse(result["expected_visible"])

    def test_overprivileged_agent_c_visibility_fails_even_if_caller_expects_it(self) -> None:
        result = self._visibility("agent-c", {"expect_visible": True}, visible=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(result["actual_visible"])


if __name__ == "__main__":
    unittest.main()
