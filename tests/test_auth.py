import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

os.environ.setdefault("LLM_URL", "http://localhost:11434/v1/chat/completions")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("TEI_URL", "http://localhost:8080")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from production_inference import api, auth, llm


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.principal = auth.AuthenticatedPrincipal(
            "tenant-a", "L3_SRE", "user-1"
        )
        api.app.dependency_overrides[api.rate_limit] = lambda: None
        api.app.dependency_overrides[api.get_current_principal] = lambda: self.principal
        self.client = TestClient(api.app)

    def tearDown(self) -> None:
        api.app.dependency_overrides.clear()

    async def test_valid_signed_token_produces_trusted_principal(self) -> None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(
            {
                "sub": "user-1",
                "tenant_id": "tenant-a",
                "role": "L3_SRE",
                "iss": "https://issuer.example.test/",
                "aud": "production-inference",
                "exp": 4_102_444_800,
            },
            private_key,
            algorithm="RS256",
        )
        jwks_client = SimpleNamespace(
            get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private_key.public_key())
        )

        with (
            patch.object(auth, "AUTH_JWKS_URL", "https://issuer.example.test/.well-known/jwks.json"),
            patch.object(auth, "AUTH_ISSUER", "https://issuer.example.test/"),
            patch.object(auth, "AUTH_AUDIENCE", "production-inference"),
            patch.object(auth, "get_jwks_client", return_value=jwks_client),
        ):
            principal = await auth.get_current_principal(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
            )

        self.assertEqual(principal.tenant_id, "tenant-a")
        self.assertEqual(principal.user_role, "L3_SRE")
        self.assertEqual(principal.subject, "user-1")

    async def test_missing_credentials_are_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await auth.get_current_principal(None)

        self.assertEqual(raised.exception.status_code, 401)

    async def test_unconfigured_identity_provider_fails_closed(self) -> None:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token")
        with (
            patch.object(auth, "AUTH_JWKS_URL", None),
            patch.object(auth, "AUTH_ISSUER", None),
            patch.object(auth, "AUTH_AUDIENCE", None),
            self.assertRaises(HTTPException) as raised,
        ):
            await auth.get_current_principal(credentials)

        self.assertEqual(raised.exception.status_code, 503)

    async def test_development_token_authenticates_only_in_explicit_dev_mode(self) -> None:
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="local-dev-token"
        )
        with (
            patch.object(auth, "APP_ENV", "development"),
            patch.object(auth, "AUTH_DEV_MODE", True),
            patch.object(auth, "AUTH_DEV_TOKEN", "local-dev-token"),
            patch.object(auth, "AUTH_DEV_TENANT_ID", "local-dev-tenant"),
            patch.object(auth, "AUTH_DEV_ROLE", "L1_Helpdesk"),
        ):
            principal = await auth.get_current_principal(credentials)

        self.assertEqual(principal.tenant_id, "local-dev-tenant")
        self.assertEqual(principal.user_role, "L1_Helpdesk")

    async def test_development_token_is_not_enabled_outside_development(self) -> None:
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="local-dev-token"
        )
        with (
            patch.object(auth, "APP_ENV", "production"),
            patch.object(auth, "AUTH_DEV_MODE", True),
            patch.object(auth, "AUTH_DEV_TOKEN", "local-dev-token"),
            patch.object(auth, "AUTH_JWKS_URL", None),
            patch.object(auth, "AUTH_ISSUER", None),
            patch.object(auth, "AUTH_AUDIENCE", None),
            self.assertRaises(HTTPException) as raised,
        ):
            await auth.get_current_principal(credentials)

        self.assertEqual(raised.exception.status_code, 503)

    async def test_wrong_development_token_is_rejected(self) -> None:
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="wrong-token"
        )
        with (
            patch.object(auth, "APP_ENV", "development"),
            patch.object(auth, "AUTH_DEV_MODE", True),
            patch.object(auth, "AUTH_DEV_TOKEN", "local-dev-token"),
            self.assertRaises(HTTPException) as raised,
        ):
            await auth.get_current_principal(credentials)

        self.assertEqual(raised.exception.status_code, 401)

    async def test_request_claims_cannot_select_unknown_role(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            auth.principal_from_claims({"tenant_id": "tenant-a", "role": "admin"})

        self.assertEqual(raised.exception.status_code, 403)

    async def test_search_uses_principal_and_rejects_body_identity(self) -> None:
        with (
            patch.object(api, "get_search_context", new_callable=AsyncMock) as get_context,
        ):
            get_context.return_value = (["incident"], True)

            response = self.client.post(
                "/search",
                json={"query": "CVE-2026-0001", "tenant_id": "tenant-b", "user_role": "SecOps_Lead"},
            )

            self.assertEqual(response.status_code, 422)
            get_context.assert_not_awaited()

            response = self.client.post("/search", json={"query": "CVE-2026-21428"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hits_found"], 1)
        self.assertEqual(response.json()["safe_context"], ["incident"])
        self.assertTrue(response.json()["cache_hit"])
        get_context.assert_awaited_once_with(
            "CVE-2026-21428", self.principal, ["LOW", "MEDIUM", "HIGH"]
        )

    async def test_chat_uses_authorized_search_context_for_llm(self) -> None:
        with (
            patch.object(api, "get_search_context", new_callable=AsyncMock) as get_context,
            patch.object(api, "generate_text", new_callable=AsyncMock) as generate,
            patch.object(
                api, "load_conversation_history", new_callable=AsyncMock
            ) as load_history,
            patch.object(
                api, "append_conversation_turn", new_callable=AsyncMock
            ) as append_turn,
        ):
            get_context.return_value = (["CVE incident context"], False)
            load_history.return_value = []
            generate.return_value = "Answer grounded in the incident."

            response = self.client.post("/chat", json={"prompt": "What happened?"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hits_found"], 1)
        self.assertEqual(response.json()["safe_context"], ["CVE incident context"])
        self.assertFalse(response.json()["cache_hit"])
        conversation_id = response.json()["conversation_id"]
        self.assertTrue(UUID(conversation_id))
        self.assertEqual(
            response.json()["model_output"], "Answer grounded in the incident."
        )
        load_history.assert_awaited_once_with(
            api.redis_client,
            "tenant-a",
            "user-1",
            ["LOW", "MEDIUM", "HIGH"],
            conversation_id,
        )
        get_context.assert_awaited_once_with(
            "What happened?", self.principal, ["LOW", "MEDIUM", "HIGH"]
        )
        generate.assert_awaited_once_with(
            "What happened?", ["CVE incident context"], []
        )
        append_turn.assert_awaited_once_with(
            api.redis_client,
            "tenant-a",
            "user-1",
            ["LOW", "MEDIUM", "HIGH"],
            conversation_id,
            "What happened?",
            "Answer grounded in the incident.",
        )

    async def test_chat_reuses_conversation_history_and_contextualizes_retrieval(self) -> None:
        conversation_id = str(uuid4())
        history = [
            {"role": "user", "content": "Tell me about CVE-2026-21428"},
            {"role": "assistant", "content": "It affects cpp-httplib headers."},
        ]
        with (
            patch.object(api, "get_search_context", new_callable=AsyncMock) as get_context,
            patch.object(api, "generate_text", new_callable=AsyncMock) as generate,
            patch.object(
                api, "load_conversation_history", new_callable=AsyncMock
            ) as load_history,
            patch.object(
                api, "append_conversation_turn", new_callable=AsyncMock
            ),
        ):
            get_context.return_value = (["Fresh CVE context"], True)
            load_history.return_value = history
            generate.return_value = "The patched version is 0.30.0."

            response = self.client.post(
                "/chat",
                json={
                    "prompt": "What version fixes it?",
                    "conversation_id": conversation_id,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["conversation_id"], conversation_id)
        load_history.assert_awaited_once_with(
            api.redis_client,
            "tenant-a",
            "user-1",
            ["LOW", "MEDIUM", "HIGH"],
            conversation_id,
        )
        get_context.assert_awaited_once_with(
            "Tell me about CVE-2026-21428\nWhat version fixes it?",
            self.principal,
            ["LOW", "MEDIUM", "HIGH"],
        )
        generate.assert_awaited_once_with(
            "What version fixes it?", ["Fresh CVE context"], history
        )

    async def test_chat_requires_authenticated_principal(self) -> None:
        api.app.dependency_overrides.pop(api.get_current_principal)

        response = self.client.post("/chat", json={"prompt": "What happened?"})

        self.assertEqual(response.status_code, 401)

    async def test_llm_request_contains_retrieved_context_and_question(self) -> None:
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "choices": [{"message": {"content": "Grounded answer"}}]
            },
        )
        http_client = AsyncMock()
        http_client.post.return_value = response
        client_factory = unittest.mock.MagicMock()
        client_factory.return_value.__aenter__ = AsyncMock(return_value=http_client)
        client_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch.object(llm.httpx, "AsyncClient", client_factory):
            answer = await llm.generate_text(
                "What happened?",
                ["CVE incident context"],
                [
                    {"role": "user", "content": "Tell me about the CVE."},
                    {"role": "assistant", "content": "It affects headers."},
                ],
            )

        self.assertEqual(answer, "Grounded answer")
        payload = http_client.post.await_args.kwargs["json"]
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIn("untrusted reference data", payload["messages"][0]["content"])
        self.assertEqual(
            payload["messages"][1],
            {"role": "user", "content": "Incident context:\nCVE incident context"},
        )
        self.assertEqual(
            payload["messages"][2],
            {"role": "user", "content": "Tell me about the CVE."},
        )
        self.assertEqual(
            payload["messages"][3],
            {"role": "assistant", "content": "It affects headers."},
        )
        self.assertEqual(
            payload["messages"][4],
            {"role": "user", "content": "What happened?"},
        )


if __name__ == "__main__":
    unittest.main()