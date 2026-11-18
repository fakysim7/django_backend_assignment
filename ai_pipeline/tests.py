import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient
from requests import HTTPError, Timeout
from django.utils import timezone

from main.models import Projects

from .models import (
    AIPlan,
    AIOperation,
    AIProviderAttempt,
    AIPipeline,
    PipelineVersion,
    TokenLedgerEntry,
    TokenWallet,
    UserEntitlement,
)
from .services import (
    PipelineError,
    RetryablePipelineError,
    _claim_operation,
    _refund,
    _settle_charge,
    _call_openrouter,
    cancel_operation,
    create_operation,
    grant_tokens,
    process_operation,
    run_worker_once,
)


User = get_user_model()


class AIPipelineTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="writer",
            user_name="writer",
            email="writer@example.test",
            password="test-password",
        )
        self.other_user = User.objects.create_user(
            username="other",
            user_name="other",
            email="other@example.test",
            password="test-password",
        )
        self.plan = AIPlan.objects.create(
            code="test",
            name="Test",
            features=["structure", "subsections", "content_plan", "text", "table", "chart", "diagram"],
            daily_operation_limit=10,
            max_operation_cost=25,
        )
        UserEntitlement.objects.create(user=self.user, plan=self.plan)
        self.wallet = TokenWallet.objects.create(user=self.user, balance=30)
        self.project = Projects.objects.create(
            user=self.user,
            name="Тестовая работа",
            description="",
            type="course",
            status="active",
        )
        self.pipeline = AIPipeline.objects.create(
            project=self.project,
            user=self.user,
            status=AIPipeline.Status.COLLECTING_REQUIREMENTS,
            requirements={"topic": "Тестовая работа"},
        )

    def test_idempotent_enqueue_reserves_tokens_once(self):
        operation, created = create_operation(
            self.pipeline, self.user, "structure", {}, "request-1"
        )
        retry, retry_created = create_operation(
            self.pipeline, self.user, "structure", {}, "request-1"
        )

        self.wallet.refresh_from_db()
        self.assertTrue(created)
        self.assertFalse(retry_created)
        self.assertEqual(operation.pk, retry.pk)
        self.assertEqual(self.wallet.balance, 25)
        self.assertEqual(
            TokenLedgerEntry.objects.filter(operation=operation).count(),
            1,
        )

    def test_failed_operation_refunds_reserved_tokens(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "request-fail"
        )

        with patch(
            "ai_pipeline.services._call_openrouter",
            side_effect=PipelineError("provider unavailable"),
        ):
            process_operation(operation.pk)

        operation.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(operation.status, AIOperation.Status.FAILED)
        self.assertEqual(self.wallet.balance, 30)
        self.assertEqual(
            sum(entry.amount for entry in TokenLedgerEntry.objects.filter(operation=operation)),
            0,
        )

    def test_successful_generation_is_versioned_before_confirmation(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "request-success"
        )
        outcome = {
            "result": {"chapters": [{"title": "Введение", "order": 1}]},
            "model": "configured-test-model",
            "input_tokens": 25,
            "output_tokens": 15,
            "provider_cost_usd": Decimal("0"),
        }

        with patch("ai_pipeline.services._call_openrouter", return_value=outcome):
            process_operation(operation.pk)

        operation.refresh_from_db()
        self.pipeline.refresh_from_db()
        self.wallet.refresh_from_db()
        version = PipelineVersion.objects.get(
            pipeline=self.pipeline, stage="structure", version=1
        )
        self.assertEqual(operation.status, AIOperation.Status.SUCCEEDED)
        self.assertEqual(operation.charged_tokens, 5)
        self.assertEqual(self.wallet.balance, 25)
        self.assertEqual(self.pipeline.structure, [])
        self.assertEqual(version.data, outcome["result"]["chapters"])

    def test_user_cannot_read_another_users_pipeline(self):
        client = APIClient()
        client.force_authenticate(self.other_user)

        response = client.get(f"/api/ai/projects/{self.project.id}/")

        self.assertEqual(response.status_code, 404)

    def test_auth_routes_support_login_refresh_and_session_restoration(self):
        self.user.set_password("test-password")
        self.user.save(update_fields=["password"])
        client = APIClient()

        login = client.post(
            "/api/login/",
            {"login": self.user.email, "password": "test-password"},
            format="json",
        )

        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.data["user"]["id"], str(self.user.user_id))
        refresh = client.post("/api/token/refresh/", {}, format="json")
        self.assertEqual(refresh.status_code, 200)
        self.assertIn("access", refresh.data)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.data['access']}")
        restored_user = client.get("/api/me/")
        self.assertEqual(restored_user.status_code, 200)
        self.assertEqual(restored_user.data["id"], str(self.user.user_id))

    def test_register_route_sets_refresh_cookie_and_me_alias(self):
        client = APIClient()
        registration = client.post(
            "/api/register/",
            {
                "user_name": "new-writer",
                "email": "new-writer@example.test",
                "password": "test-password",
            },
            format="json",
        )

        self.assertEqual(registration.status_code, 201)
        self.assertEqual(registration.data["user"]["user_name"], "new-writer")
        self.assertIn("refresh_token", client.cookies)
        refresh = client.post("/api/token/refresh/", {}, format="json")
        self.assertEqual(refresh.status_code, 200)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.data['access']}")
        me_response = client.get("/api/me/")
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.data["email"], "new-writer@example.test")

    def test_google_auth_legacy_and_api_aliases_resolve(self):
        client = APIClient()

        api_response = client.post("/api/google_auth/", {}, format="json")
        legacy_response = client.post("/google/", {}, format="json")

        self.assertEqual(api_response.status_code, 400)
        self.assertEqual(legacy_response.status_code, 400)

    def test_invalid_chart_payload_is_rejected(self):
        from .services import _validate_result

        with self.assertRaises(PipelineError):
            _validate_result(
                "chart",
                {
                    "chart_type": "pie",
                    "series": [{"name": "series", "points": [{"x": "A", "y": "not a number"}]}],
                    "is_demonstration": False,
                },
            )

    def test_api_block_generation_preserves_block_id_through_worker(self):
        block_id = "planned-text-block"
        self.pipeline.confirmed_steps = ["structure", "subsections", "content_plan"]
        self.pipeline.content_plan = [
            {
                "chapter_title": "Глава",
                "subsection_title": "Раздел",
                "blocks": [
                    {
                        "id": block_id,
                        "type": "text",
                        "title": "Содержание",
                        "description": "Сгенерировать текст",
                        "paragraphs": 2,
                    }
                ],
            }
        ]
        self.pipeline.save()
        self.project.blocks = [
            {
                "id": block_id,
                "type": "text",
                "source": "ai_pipeline",
                "content": {"text": ""},
            }
        ]
        self.project.save(update_fields=["blocks"])

        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(
            f"/api/ai/projects/{self.project.id}/operations/",
            {"task_type": "text", "payload": {"block_id": block_id}},
            format="json",
            HTTP_IDEMPOTENCY_KEY="block-generation-1",
        )

        self.assertEqual(response.status_code, 202)
        operation = AIOperation.objects.get(pk=response.data["id"])
        outcome = {
            "result": {"paragraphs": ["Сгенерированный текст"]},
            "model": "configured-test-model",
            "input_tokens": 25,
            "output_tokens": 15,
            "provider_cost_usd": Decimal("0"),
        }
        with patch("ai_pipeline.services._call_openrouter", return_value=outcome):
            process_operation(operation.pk)

        self.project.refresh_from_db()
        self.assertEqual(
            self.project.blocks[0]["content"]["text"],
            "Сгенерированный текст",
        )

    def test_refund_is_idempotent(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "refund-once"
        )

        self.assertTrue(_refund(operation, "test refund"))
        self.assertFalse(_refund(operation, "duplicate refund"))

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 30)
        self.assertEqual(
            TokenLedgerEntry.objects.filter(
                idempotency_key=f"refund:{operation.id}"
            ).count(),
            1,
        )

    def test_grant_is_idempotent_and_conflicts_do_not_change_balance(self):
        balance = grant_tokens(self.user, 5, "test grant", "grant-once")
        repeated_balance = grant_tokens(
            self.user, 5, "repeated grant", "grant-once"
        )

        with self.assertRaises(PipelineError):
            grant_tokens(self.user, 10, "conflicting grant", "grant-once")

        self.wallet.refresh_from_db()
        self.assertEqual(balance, 35)
        self.assertEqual(repeated_balance, 35)
        self.assertEqual(self.wallet.balance, 35)
        self.assertEqual(
            TokenLedgerEntry.objects.filter(idempotency_key="grant-once").count(),
            1,
        )

    def test_settlement_is_idempotent(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "settlement-once"
        )

        with self.subTest("first settlement"):
            charge = _settle_charge(operation, self.wallet, Decimal("0.001"))
        repeated_charge = _settle_charge(operation, self.wallet, Decimal("0.001"))

        self.wallet.refresh_from_db()
        self.assertEqual(charge, 2)
        self.assertEqual(repeated_charge, charge)
        self.assertEqual(self.wallet.balance, 28)
        self.assertEqual(
            TokenLedgerEntry.objects.filter(
                idempotency_key=f"settlement-refund:{operation.id}"
            ).count(),
            1,
        )

    def test_settlement_rolls_back_balance_and_ledger_together(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "settlement-rollback"
        )

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                _settle_charge(operation, self.wallet, Decimal("0.001"))
                raise RuntimeError("simulate failure before operation commit")

        self.wallet.refresh_from_db()
        reservation = TokenLedgerEntry.objects.get(
            idempotency_key=f"reserve:{operation.id}"
        )
        self.assertEqual(self.wallet.balance, 25)
        self.assertEqual(reservation.kind, TokenLedgerEntry.Kind.RESERVED)
        self.assertFalse(
            TokenLedgerEntry.objects.filter(
                idempotency_key=f"settlement-refund:{operation.id}"
            ).exists()
        )

    def test_worker_recovers_an_expired_running_operation(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "recover-expired"
        )
        operation.status = AIOperation.Status.RUNNING
        operation.worker_attempts = 1
        operation.lease_expires_at = timezone.now() - timedelta(seconds=1)
        operation.save(
            update_fields=["status", "worker_attempts", "lease_expires_at"]
        )
        abandoned_attempt = AIProviderAttempt.objects.create(
            operation=operation,
            attempt_number=1,
            model_name="interrupted-model",
            status=AIProviderAttempt.Status.RUNNING,
        )
        outcome = {
            "result": {"chapters": [{"title": "Введение", "order": 1}]},
            "model": "configured-test-model",
            "input_tokens": 25,
            "output_tokens": 15,
            "provider_cost_usd": Decimal("0"),
        }

        with patch("ai_pipeline.services._call_openrouter", return_value=outcome):
            self.assertTrue(run_worker_once())

        operation.refresh_from_db()
        self.assertEqual(operation.status, AIOperation.Status.SUCCEEDED)
        self.assertEqual(operation.worker_attempts, 2)
        self.assertIsNone(operation.lease_owner)
        self.assertIsNone(operation.lease_expires_at)
        abandoned_attempt.refresh_from_db()
        self.assertEqual(
            abandoned_attempt.status, AIProviderAttempt.Status.FAILED
        )

    def test_operation_can_only_be_claimed_once_while_lease_is_active(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "claim-once"
        )

        first_claim = _claim_operation(operation.id)
        second_claim = _claim_operation(operation.id)

        self.assertIsNotNone(first_claim)
        self.assertIsNone(second_claim)
        operation.refresh_from_db()
        self.assertEqual(operation.worker_attempts, 1)

    def test_running_cancellation_refunds_once_even_if_worker_never_returns(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "cancel-running"
        )
        operation.status = AIOperation.Status.RUNNING
        operation.lease_expires_at = timezone.now() + timedelta(minutes=5)
        operation.save(
            update_fields=["status", "lease_expires_at"]
        )
        attempt = AIProviderAttempt.objects.create(
            operation=operation,
            attempt_number=1,
            model_name="test-model",
            status=AIProviderAttempt.Status.RUNNING,
        )

        cancel_operation(operation)
        cancel_operation(operation)

        operation.refresh_from_db()
        attempt.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(operation.status, AIOperation.Status.CANCELLED)
        self.assertIsNone(operation.lease_expires_at)
        self.assertEqual(attempt.status, AIProviderAttempt.Status.FAILED)
        self.assertEqual(self.wallet.balance, 30)
        self.assertEqual(
            TokenLedgerEntry.objects.filter(
                idempotency_key=f"refund:{operation.id}"
            ).count(),
            1,
        )

    def test_repeated_processing_does_not_repeat_a_succeeded_operation(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "process-once"
        )
        outcome = {
            "result": {"chapters": [{"title": "Введение", "order": 1}]},
            "model": "configured-test-model",
            "input_tokens": 25,
            "output_tokens": 15,
            "provider_cost_usd": Decimal("0"),
        }

        with patch(
            "ai_pipeline.services._call_openrouter", return_value=outcome
        ) as call:
            self.assertTrue(process_operation(operation.id))
            self.assertFalse(process_operation(operation.id))

        self.assertEqual(call.call_count, 1)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 25)

    def test_transient_worker_failure_retries_then_succeeds(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "worker-retry"
        )
        outcome = {
            "result": {"chapters": [{"title": "Введение", "order": 1}]},
            "model": "configured-test-model",
            "input_tokens": 25,
            "output_tokens": 15,
            "provider_cost_usd": Decimal("0"),
        }

        with patch(
            "ai_pipeline.services._call_openrouter",
            side_effect=[RetryablePipelineError("temporary"), outcome],
        ):
            run_worker_once()
            operation.refresh_from_db()
            self.assertEqual(operation.status, AIOperation.Status.QUEUED)
            run_worker_once()

        operation.refresh_from_db()
        self.assertEqual(operation.status, AIOperation.Status.SUCCEEDED)
        self.assertEqual(operation.worker_attempts, 2)

    @override_settings(AI_WORKER_MAX_ATTEMPTS=2)
    def test_worker_exhaustion_fails_and_refunds(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "worker-exhausted"
        )
        operation.worker_attempts = 1
        operation.save(update_fields=["worker_attempts"])

        with patch(
            "ai_pipeline.services._call_openrouter",
            side_effect=RetryablePipelineError("provider unavailable"),
        ):
            run_worker_once()

        operation.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(operation.status, AIOperation.Status.FAILED)
        self.assertEqual(operation.worker_attempts, 2)
        self.assertEqual(self.wallet.balance, 30)

    @staticmethod
    def _provider_response(body, status_code=200):
        response = Mock()
        response.status_code = status_code
        response.json.return_value = body
        if status_code >= 400:
            response.raise_for_status.side_effect = HTTPError(
                f"provider returned {status_code}", response=response
            )
        return response

    @override_settings(
        OPENROUTER_API_KEY="test-key",
        OPENROUTER_MODEL_ROUTING_JSON=(
            '{"structure":{"model":"first-model","fallbacks":["second-model"]}}'
        ),
        AI_PROVIDER_RETRY_ATTEMPTS=1,
    )
    def test_invalid_json_falls_back_and_keeps_each_model_cost(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "router-fallback-cost"
        )
        invalid_json = self._provider_response(
            {"usage": {"cost": "0.02"}, "choices": [{"message": {"content": "{"}}]}
        )
        valid_json = self._provider_response(
            {
                "usage": {
                    "cost": "0.03",
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                },
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"chapters": [{"title": "Introduction", "order": 1}]}
                            )
                        }
                    }
                ],
            }
        )

        with patch(
            "ai_pipeline.services.requests.post",
            side_effect=[invalid_json, valid_json],
        ):
            outcome = _call_openrouter(operation)

        attempts = list(operation.provider_attempts.all())
        self.assertEqual(outcome["model"], "second-model")
        self.assertEqual(outcome["provider_cost_usd"], Decimal("0.05"))
        self.assertEqual(
            [(attempt.model_name, attempt.status) for attempt in attempts],
            [
                ("first-model", AIProviderAttempt.Status.FAILED),
                ("second-model", AIProviderAttempt.Status.SUCCEEDED),
            ],
        )
        self.assertEqual(attempts[0].provider_cost_usd, Decimal("0.02"))
        self.assertEqual(attempts[1].provider_cost_usd, Decimal("0.03"))

    @override_settings(
        OPENROUTER_API_KEY="test-key",
        OPENROUTER_MODEL_ROUTING_JSON=(
            '{"structure":{"model":"first-model","fallbacks":["second-model"]}}'
        ),
        AI_PROVIDER_RETRY_ATTEMPTS=1,
    )
    def test_provider_cost_remains_unknown_when_any_attempt_cost_is_missing(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "router-unknown-cost"
        )
        failed = self._provider_response(
            {"usage": {"cost": "0.02"}, "choices": [{"message": {"content": "{"}}]}
        )
        successful_without_cost = self._provider_response(
            {
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"chapters": [{"title": "Introduction", "order": 1}]}
                            )
                        }
                    }
                ],
            }
        )

        with patch(
            "ai_pipeline.services.requests.post",
            side_effect=[failed, successful_without_cost],
        ):
            outcome = _call_openrouter(operation)

        self.assertIsNone(outcome["provider_cost_usd"])
        self.assertIsNone(
            operation.provider_attempts.get(
                status=AIProviderAttempt.Status.SUCCEEDED
            ).provider_cost_usd
        )

    @override_settings(
        OPENROUTER_API_KEY="test-key",
        OPENROUTER_MODEL_ROUTING_JSON='{"structure":{"model":"one-model"}}',
        AI_PROVIDER_RETRY_ATTEMPTS=2,
    )
    def test_provider_retries_are_bounded_and_classified(self):
        operation, _ = create_operation(
            self.pipeline, self.user, "structure", {}, "provider-retry-limit"
        )

        with patch(
            "ai_pipeline.services.requests.post",
            side_effect=[Timeout("timed out"), Timeout("timed out")],
        ) as provider:
            with self.assertRaises(RetryablePipelineError):
                _call_openrouter(operation)

        self.assertEqual(provider.call_count, 2)
        self.assertEqual(operation.provider_attempts.count(), 2)
        self.assertTrue(
            all(
                attempt.is_retryable
                for attempt in operation.provider_attempts.all()
            )
        )


class AIPipelineWorkerConcurrencyTests(TransactionTestCase):
    def test_parallel_workers_cannot_claim_the_same_operation(self):
        user = User.objects.create_user(
            username="parallel-writer",
            user_name="parallel-writer",
            email="parallel@example.test",
            password="test-password",
        )
        plan = AIPlan.objects.create(
            code="parallel",
            name="Parallel test",
            features=["structure"],
            daily_operation_limit=10,
            max_operation_cost=25,
        )
        UserEntitlement.objects.create(user=user, plan=plan)
        TokenWallet.objects.create(user=user, balance=30)
        project = Projects.objects.create(
            user=user,
            name="Parallel test",
            description="",
            type="course",
            status="active",
        )
        pipeline = AIPipeline.objects.create(
            project=project,
            user=user,
            status=AIPipeline.Status.COLLECTING_REQUIREMENTS,
            requirements={"topic": "Parallel test"},
        )
        operation, _ = create_operation(
            pipeline, user, "structure", {}, "parallel-claim"
        )
        barrier = threading.Barrier(2)

        def claim():
            close_old_connections()
            try:
                barrier.wait()
                return _claim_operation(operation.id)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _: claim(), range(2)))

        operation.refresh_from_db()
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        self.assertEqual(operation.worker_attempts, 1)
