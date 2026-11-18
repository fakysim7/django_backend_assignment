from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import close_old_connections, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from platform_admin.models import PlatformAdminAccount
from platform_admin.roles import ROLE_GROUP_NAMES
from support.models import Ticket, TicketMessage
from support.notification_events import notify_ticket_message

from .models import Notification, OutboxEvent
from .services import enqueue_in_app_notification
from .worker import MAX_ATTEMPTS, run_worker_once

User = get_user_model()


class NotificationSystemTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = self.create_user("notification-owner")
        self.other = self.create_user("notification-other")
        self.support = self.create_user("notification-support")
        self.support.groups.add(Group.objects.get(name=ROLE_GROUP_NAMES["Support"]))

    def create_user(self, username):
        return User.objects.create_user(
            username=username,
            user_name=username,
            email=f"{username}@example.test",
            password="test-password",
        )

    def client_as(self, user):
        client = APIClient()
        account, _ = PlatformAdminAccount.objects.get_or_create(
            rbac_user=user,
            defaults={"username": f"test-{user.user_name}"},
        )
        account.set_password("NotificationAdminPass123!")
        account.save()
        csrf = client.get("/api/admin/auth/csrf/")
        access = str(RefreshToken.for_user(user).access_token)
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {access}",
            HTTP_X_CSRFTOKEN=csrf.data["csrfToken"],
        )
        login = client.post(
            "/api/admin/auth/login/",
            {
                "username": account.username,
                "password": "NotificationAdminPass123!",
            },
            format="json",
        )
        if login.status_code != 200:
            raise AssertionError("Could not establish test admin session.")
        return client

    def enqueue(self, key="test-event", recipient=None):
        with transaction.atomic():
            return enqueue_in_app_notification(
                recipient_id=(recipient or self.owner).pk,
                event_type="test.event",
                idempotency_key=key,
                title="Test notification",
                message="A safe notification.",
                route="/support",
                payload={"ticket_id": "ticket-123"},
            )

    def test_worker_creates_one_notification_and_repeated_processing_is_idempotent(self):
        event = self.enqueue()
        duplicate_event = self.enqueue()
        self.assertEqual(event.pk, duplicate_event.pk)
        self.assertTrue(run_worker_once())
        self.assertFalse(run_worker_once())
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxEvent.STATUS_PROCESSED)
        self.assertEqual(event.attempts, 1)
        self.assertEqual(Notification.objects.filter(outbox_event=event).count(), 1)

    def test_shared_outbox_accepts_safe_metadata_for_other_domains(self):
        with transaction.atomic():
            event = enqueue_in_app_notification(
                recipient_id=self.owner.pk,
                event_type="projects.export_ready",
                idempotency_key="project-export-1",
                title="Экспорт готов",
                message="Ваш документ готов к скачиванию.",
                route="/projects",
                payload={"object_type": "project", "object_id": "project-123"},
            )

        self.assertTrue(run_worker_once())
        notification = Notification.objects.get(outbox_event=event)
        self.assertEqual(
            notification.payload,
            {"object_type": "project", "object_id": "project-123"},
        )

    def test_worker_retries_temporarily_and_caps_attempts(self):
        event = self.enqueue("retry-event")
        with patch("notifications.worker._deliver", side_effect=RuntimeError("private content")):
            for attempt in range(MAX_ATTEMPTS):
                if attempt:
                    OutboxEvent.objects.filter(pk=event.pk).update(
                        next_attempt_at=timezone.now() - timedelta(seconds=1)
                    )
                self.assertTrue(run_worker_once())
                event.refresh_from_db()
            self.assertEqual(event.status, OutboxEvent.STATUS_FAILED)
            self.assertEqual(event.attempts, MAX_ATTEMPTS)
            self.assertNotIn("private content", event.last_error)
            self.assertIsNotNone(event.last_error)

    def test_worker_recovers_stale_processing_event(self):
        event = self.enqueue("stale-event")
        OutboxEvent.objects.filter(pk=event.pk).update(
            status=OutboxEvent.STATUS_PROCESSING,
            attempts=1,
            processing_started_at=timezone.now() - timedelta(minutes=6),
        )
        self.assertTrue(run_worker_once())
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxEvent.STATUS_PROCESSED)
        self.assertEqual(event.attempts, 2)

    def test_notifications_are_private_readable_and_read_actions_are_idempotent(self):
        event = self.enqueue()
        run_worker_once()
        notification = Notification.objects.get(outbox_event=event)
        owner_client = APIClient()
        owner_client.force_authenticate(self.owner)
        other_client = APIClient()
        other_client.force_authenticate(self.other)

        listing = owner_client.get("/api/notifications/?read=false")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 1)
        self.assertNotIn("outbox_event", listing.data["results"][0])
        self.assertEqual(owner_client.get("/api/notifications/unread-count/").data, {"count": 1})
        self.assertEqual(other_client.get("/api/notifications/").data["count"], 0)
        self.assertEqual(
            other_client.post(f"/api/notifications/{notification.id}/read/").status_code,
            404,
        )

        read_response = owner_client.post(f"/api/notifications/{notification.id}/read/")
        first_read_at = read_response.data["read_at"]
        second_read = owner_client.post(f"/api/notifications/{notification.id}/read/")
        self.assertEqual(second_read.data["read_at"], first_read_at)
        self.assertEqual(owner_client.get("/api/notifications/unread-count/").data, {"count": 0})

        unread = self.enqueue("second-event")
        run_worker_once()
        self.assertEqual(owner_client.post("/api/notifications/read-all/").data, {"updated": 1})
        unread.refresh_from_db()
        self.assertTrue(Notification.objects.get(outbox_event=unread).read_at)

    def test_support_message_events_exclude_internal_notes_and_private_content(self):
        ticket = Ticket.objects.create(
            user=self.owner,
            title="Private ticket title",
            description="Private ticket description",
        )
        internal = TicketMessage.objects.create(
            ticket=ticket,
            author=self.support,
            text="Private internal note",
            is_internal=True,
        )
        with transaction.atomic():
            notify_ticket_message(ticket, internal)
        self.assertFalse(OutboxEvent.objects.exists())

        public = TicketMessage.objects.create(
            ticket=ticket,
            author=self.support,
            text="Private reply body",
            is_internal=False,
        )
        with transaction.atomic():
            notify_ticket_message(ticket, public)
        event = OutboxEvent.objects.get()
        self.assertEqual(event.payload["recipient_id"], str(self.owner.pk))
        self.assertNotIn("Private reply body", str(event.payload))
        self.assertNotIn("Private ticket title", str(event.payload))

    def test_user_message_falls_back_to_active_support_when_assignee_is_inactive(self):
        inactive_assignee = self.create_user("inactive-support")
        inactive_assignee.groups.add(Group.objects.get(name=ROLE_GROUP_NAMES["Support"]))
        inactive_assignee.is_active = False
        inactive_assignee.save(update_fields=("is_active",))
        ticket = Ticket.objects.create(
            user=self.owner,
            title="Ticket",
            description="Description",
            assignee=inactive_assignee,
        )
        message = TicketMessage.objects.create(
            ticket=ticket,
            author=self.owner,
            text="A public message.",
        )

        with transaction.atomic():
            notify_ticket_message(ticket, message)

        self.assertEqual(OutboxEvent.objects.count(), 1)
        event = OutboxEvent.objects.get()
        self.assertEqual(event.payload["recipient_id"], str(self.support.pk))

    def test_support_actions_enqueue_the_expected_staff_and_user_events(self):
        owner_client = self.client_as(self.owner)
        support_client = self.client_as(self.support)

        created = owner_client.post(
            "/api/support/tickets/",
            {
                "title": "Ticket title",
                "description": "Ticket description",
                "category": "technical",
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        ticket_id = created.data["id"]
        event = OutboxEvent.objects.get(event_type="support.ticket.created")
        self.assertEqual(event.payload["recipient_id"], str(self.support.pk))

        user_message = owner_client.post(
            f"/api/support/tickets/{ticket_id}/messages/",
            {"text": "Private user message"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="user-message-1",
        )
        self.assertEqual(user_message.status_code, 201)
        self.assertTrue(
            OutboxEvent.objects.filter(
                event_type="support.ticket.user_message",
                payload__recipient_id=str(self.support.pk),
            ).exists()
        )

        note = support_client.post(
            f"/api/admin/support/tickets/{ticket_id}/notes/",
            {"text": "Private note"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="internal-note-1",
        )
        self.assertEqual(note.status_code, 201)
        event_count = OutboxEvent.objects.count()

        reply = support_client.post(
            f"/api/admin/support/tickets/{ticket_id}/messages/",
            {"text": "Private public reply"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="public-reply-1",
        )
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(OutboxEvent.objects.count(), event_count + 2)
        self.assertTrue(
            OutboxEvent.objects.filter(
                event_type="support.ticket.public_reply",
                payload__recipient_id=str(self.owner.pk),
            ).exists()
        )
        self.assertTrue(
            OutboxEvent.objects.filter(
                event_type="support.ticket.status_changed",
                payload__recipient_id=str(self.owner.pk),
            ).exists()
        )

        assignee = self.create_user("notification-assignee")
        assignee.groups.add(Group.objects.get(name=ROLE_GROUP_NAMES["Support"]))
        assigned = support_client.post(
            f"/api/admin/support/tickets/{ticket_id}/assignment/",
            {"assignee_id": str(assignee.user_id)},
            format="json",
        )
        self.assertEqual(assigned.status_code, 200)
        self.assertTrue(
            OutboxEvent.objects.filter(
                event_type="support.ticket.assigned",
                payload__recipient_id=str(assignee.pk),
            ).exists()
        )

        resolved = support_client.post(
            f"/api/admin/support/tickets/{ticket_id}/status/",
            {"status": Ticket.STATUS_RESOLVED},
            format="json",
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertTrue(
            OutboxEvent.objects.filter(
                event_type="support.ticket.resolved",
                payload__recipient_id=str(self.owner.pk),
            ).exists()
        )

        event_payloads = list(OutboxEvent.objects.values_list("payload", flat=True))
        self.assertNotIn("Private note", str(event_payloads))
        self.assertNotIn("Private public reply", str(event_payloads))


class OutboxTransactionTests(TransactionTestCase):
    def test_outbox_requires_atomic_transaction_and_rolls_back(self):
        user = User.objects.create_user(
            username="transaction-owner",
            user_name="transaction-owner",
            email="transaction-owner@example.test",
            password="test-password",
        )
        with self.assertRaises(RuntimeError):
            enqueue_in_app_notification(
                recipient_id=user.pk,
                event_type="test.event",
                idempotency_key="outside-transaction",
                title="Test",
                message="Test",
            )

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                enqueue_in_app_notification(
                    recipient_id=user.pk,
                    event_type="test.event",
                    idempotency_key="rolled-back-event",
                    title="Test",
                    message="Test",
                )
                raise RuntimeError("rollback")
        self.assertFalse(
            OutboxEvent.objects.filter(idempotency_key="rolled-back-event").exists()
        )

    def test_concurrent_workers_claim_and_deliver_an_event_once(self):
        user = User.objects.create_user(
            username="concurrent-owner",
            user_name="concurrent-owner",
            email="concurrent-owner@example.test",
            password="test-password",
        )
        with transaction.atomic():
            event = enqueue_in_app_notification(
                recipient_id=user.pk,
                event_type="test.event",
                idempotency_key="concurrent-event",
                title="Test",
                message="Concurrent event",
            )

        barrier = Barrier(2)
        from . import worker

        original_claim = worker._claim_event

        def claim_together():
            barrier.wait(timeout=5)
            return original_claim()

        def claim_event():
            close_old_connections()
            try:
                return worker._claim_event()
            finally:
                close_old_connections()

        with patch("notifications.worker._claim_event", side_effect=claim_together):
            with ThreadPoolExecutor(max_workers=2) as executor:
                claimed_event_ids = list(executor.map(lambda _: claim_event(), range(2)))

        claim_count = claimed_event_ids.count(event.pk)
        self.assertLessEqual(claim_count, 1)
        if not claim_count:
            self.assertEqual(worker._claim_event(), event.pk)
        worker._deliver(event.pk)
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxEvent.STATUS_PROCESSED)
        self.assertEqual(event.attempts, 1)
        self.assertEqual(Notification.objects.filter(outbox_event=event).count(), 1)
