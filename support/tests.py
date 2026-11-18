from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from platform_admin.models import AdminAuditEvent, PlatformAdminAccount
from platform_admin.roles import ROLE_GROUP_NAMES

from .categories import CATEGORY_UNSPECIFIED_FILTER, SUPPORT_CATEGORIES
from .models import Ticket, TicketAttachment, TicketAssignmentHistory, TicketMessage, TicketStatusHistory

User = get_user_model()


class SupportAPITests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = self.create_user("owner")
        self.other = self.create_user("other")
        self.support = self.create_user("support-agent")
        self.moderator = self.create_user("moderator-agent")
        self.analyst = self.create_user("analyst-agent")
        self.assign_role(self.support, "Support")
        self.assign_role(self.moderator, "Moderator")
        self.assign_role(self.analyst, "Analyst")

    def create_user(self, username):
        return User.objects.create_user(
            username=username,
            user_name=username,
            email=f"{username}@example.test",
            password="SupportTestPass123!",
        )

    def assign_role(self, user, role):
        user.groups.add(Group.objects.get(name=ROLE_GROUP_NAMES[role]))

    def client_as(self, user):
        cache.clear()
        client = APIClient()
        account, _ = PlatformAdminAccount.objects.get_or_create(
            rbac_user=user,
            defaults={"username": f"test-{user.user_name}"},
        )
        account.set_password("SupportAdminPass123!")
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
                "password": "SupportAdminPass123!",
            },
            format="json",
        )
        if login.status_code != 200:
            raise AssertionError("Could not establish test admin session.")
        return client

    def create_ticket(self):
        response = self.client_as(self.owner).post(
            "/api/support/tickets/",
            {"title": "Login issue", "description": "Cannot sign in", "category": "technical"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        return Ticket.objects.get(id=response.data["id"])

    def test_category_catalog_and_ticket_creation_validation(self):
        client = self.client_as(self.owner)
        categories = client.get("/api/support/categories/")

        self.assertEqual(categories.status_code, 200)
        self.assertEqual(APIClient().get("/api/support/categories/").status_code, 401)
        self.assertEqual(
            [category["code"] for category in categories.data],
            [category["code"] for category in SUPPORT_CATEGORIES],
        )
        self.assertTrue(all({"name", "description"} <= category.keys() for category in categories.data))

        created = client.post(
            "/api/support/tickets/",
            {
                "title": "Generation question",
                "description": "Unexpected output",
                "category": "generation",
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data["category"], "generation")

        missing = client.post(
            "/api/support/tickets/",
            {"title": "Missing category", "description": "Category is required"},
            format="json",
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn("category", missing.data)

        unknown = client.post(
            "/api/support/tickets/",
            {
                "title": "Unknown category",
                "description": "Invalid code",
                "category": "billing",
            },
            format="json",
        )
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("category", unknown.data)
        self.assertIn("доступного списка", str(unknown.data["category"]))

    def test_legacy_ticket_category_is_neutral_in_user_and_admin_apis(self):
        ticket = Ticket.objects.create(
            user=self.owner,
            title="Legacy issue",
            description="Created before category selection",
            category="",
        )
        owner_client = self.client_as(self.owner)
        support_client = self.client_as(self.support)

        self.assertEqual(owner_client.get("/api/support/tickets/").data[0]["category"], "")
        self.assertEqual(
            owner_client.get(f"/api/support/tickets/{ticket.id}/").data["category"],
            "",
        )
        self.assertEqual(
            support_client.get("/api/admin/support/tickets/").data["results"][0]["category"],
            "",
        )
        self.assertEqual(
            support_client.get(f"/api/admin/support/tickets/{ticket.id}/").data["category"],
            "",
        )

        changed = owner_client.patch(
            f"/api/support/tickets/{ticket.id}/",
            {"category": "account"},
            format="json",
        )
        self.assertEqual(changed.status_code, 400)
        self.assertIn("Категорию нельзя изменить", changed.data["detail"])
        ticket.refresh_from_db()
        self.assertEqual(ticket.category, "")

    def test_category_queue_filter_combines_with_existing_filters_and_summary(self):
        technical_ticket = self.create_ticket()
        categorized_ticket = Ticket.objects.create(
            user=self.other,
            title="Other issue",
            description="Different category and status",
            category="generation",
            status=Ticket.STATUS_IN_PROGRESS,
            assignee=self.support,
        )
        unspecified_ticket = Ticket.objects.create(
            user=self.other,
            title="Legacy issue",
            description="No category recorded",
            category="",
        )
        client = self.client_as(self.support)

        response = client.get(
            "/api/admin/support/tickets/",
            {
                "category": "technical",
                "status": Ticket.STATUS_NEW,
                "assignee": "unassigned",
                "search": "Login issue",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(technical_ticket.id))
        self.assertEqual(response.data["summary"]["category_counts"]["technical"], 1)
        self.assertEqual(response.data["summary"]["category_counts"]["generation"], 0)
        self.assertEqual(response.data["summary"]["unspecified_category_count"], 0)

        unspecified = client.get(
            "/api/admin/support/tickets/",
            {"category": CATEGORY_UNSPECIFIED_FILTER},
        )
        self.assertEqual(unspecified.status_code, 200)
        self.assertEqual(unspecified.data["count"], 1)
        self.assertEqual(unspecified.data["results"][0]["id"], str(unspecified_ticket.id))
        self.assertEqual(unspecified.data["summary"]["unspecified_category_count"], 1)
        self.assertEqual(
            client.get("/api/admin/support/tickets/", {"category": "not-a-category"}).status_code,
            400,
        )
        self.assertNotEqual(categorized_ticket.id, unspecified_ticket.id)

    def test_owner_can_create_and_read_ticket_but_another_user_cannot(self):
        ticket = self.create_ticket()
        owner_client = self.client_as(self.owner)
        other_client = self.client_as(self.other)

        self.assertEqual(owner_client.get("/api/support/tickets/").data[0]["id"], str(ticket.id))
        owner_detail = owner_client.get(f"/api/support/tickets/{ticket.id}/")
        self.assertEqual(owner_detail.status_code, 200)
        for internal_field in (
            "user",
            "assignee",
            "priority",
            "resolved_at",
            "closed_at",
        ):
            self.assertNotIn(internal_field, owner_detail.data)
        self.assertEqual(other_client.get("/api/support/tickets/").data, [])
        self.assertEqual(other_client.get(f"/api/support/tickets/{ticket.id}/").status_code, 403)
        self.assertEqual(
            other_client.post(
                f"/api/support/tickets/{ticket.id}/messages/",
                {"text": "Unauthorized"},
                format="json",
            ).status_code,
            404,
        )

    def test_support_role_cannot_use_user_jwt_for_admin_queue(self):
        ticket = self.create_ticket()
        support_client = self.client_as(self.support)
        self.assertEqual(
            support_client.get("/api/support/tickets/").data,
            [],
        )
        admin_queue = support_client.get("/api/admin/support/tickets/")
        self.assertEqual(admin_queue.data["count"], 1)
        self.assertEqual(admin_queue.data["results"][0]["id"], str(ticket.id))
        self.assertEqual(
            APIClient().get("/api/admin/support/tickets/").status_code,
            401,
        )

    def test_support_queue_reply_and_internal_note_are_server_authorized(self):
        ticket = self.create_ticket()
        support_client = self.client_as(self.support)
        queue = support_client.get("/api/admin/support/tickets/")
        self.assertEqual(queue.status_code, 200)
        self.assertEqual(queue.data["count"], 1)
        self.assertEqual(queue.data["results"][0]["id"], str(ticket.id))

        detail = support_client.get(f"/api/admin/support/tickets/{ticket.id}/")
        self.assertEqual(detail.status_code, 200)
        reply = support_client.post(
            f"/api/admin/support/tickets/{ticket.id}/messages/",
            {"text": "We are looking into it"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="public-reply-1",
        )
        self.assertEqual(reply.status_code, 201)
        self.assertFalse(reply.data["is_internal"])

        note_endpoint = f"/api/admin/support/tickets/{ticket.id}/notes/"
        first_note = support_client.post(
            note_endpoint,
            {"text": "Check the auth logs"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="internal-note-1",
        )
        duplicate_note = support_client.post(
            note_endpoint,
            {"text": "Check the auth logs"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="internal-note-1",
        )
        self.assertEqual(first_note.status_code, 201)
        self.assertTrue(first_note.data["is_internal"])
        self.assertEqual(duplicate_note.status_code, 200)
        self.assertEqual(TicketMessage.objects.filter(ticket=ticket, is_internal=True).count(), 1)

        user_detail = self.client_as(self.owner).get(f"/api/support/tickets/{ticket.id}/")
        self.assertEqual(len(user_detail.data["messages"]), 1)
        self.assertEqual(user_detail.data["message_count"], 1)
        self.assertNotIn("Check the auth logs", str(user_detail.data))
        admin_detail = support_client.get(f"/api/admin/support/tickets/{ticket.id}/")
        self.assertEqual(len(admin_detail.data["messages"]), 2)
        self.assertEqual(admin_detail.data["message_count"], 2)

    def test_moderator_and_analyst_cannot_read_private_ticket_content_or_mutate_it(self):
        ticket = self.create_ticket()
        for user in (self.moderator, self.analyst):
            client = self.client_as(user)
            self.assertEqual(client.get("/api/admin/support/tickets/").status_code, 403)
            self.assertEqual(client.get(f"/api/admin/support/tickets/{ticket.id}/").status_code, 403)
            self.assertEqual(
                client.post(
                    f"/api/admin/support/tickets/{ticket.id}/notes/",
                    {"text": "Private"},
                    format="json",
                ).status_code,
                403,
            )
        upload = self.client_as(self.owner).post(
            f"/api/support/tickets/{ticket.id}/attachments/",
            {"file": SimpleUploadedFile("restricted.txt", b"private", content_type="text/plain")},
            format="multipart",
        )
        self.assertEqual(upload.status_code, 201)
        attachment = TicketAttachment.objects.get(id=upload.data["id"])
        self.assertIsNotNone(attachment.stored_file_id)
        download_url = (
            f"/api/support/tickets/{ticket.id}/attachments/{attachment.id}/"
        )
        for user in (self.moderator, self.analyst):
            self.assertEqual(self.client_as(user).get(download_url).status_code, 403)

    def test_queue_permission_does_not_expose_conversation_or_internal_notes(self):
        ticket = self.create_ticket()
        TicketMessage.objects.create(
            ticket=ticket,
            author=self.support,
            text="Staff-only note",
            is_internal=True,
        )
        support_group = Group.objects.get(name=ROLE_GROUP_NAMES["Support"])
        view_ticket_permission = Permission.objects.get(
            content_type__app_label="platform_admin",
            codename="view_support_tickets",
        )
        support_group.permissions.remove(view_ticket_permission)

        support_client = self.client_as(self.support)
        queue = support_client.get("/api/admin/support/tickets/")
        self.assertEqual(queue.status_code, 200)
        row = queue.data["results"][0]
        self.assertNotIn("messages", row)
        self.assertNotIn("description", row)
        self.assertNotIn("attachments", row)
        self.assertEqual(row["message_count"], 0)
        self.assertEqual(
            support_client.get(f"/api/admin/support/tickets/{ticket.id}/").status_code,
            403,
        )

    def test_attachment_download_is_private_and_size_and_type_are_checked(self):
        ticket = self.create_ticket()
        owner_client = self.client_as(self.owner)
        upload_url = f"/api/support/tickets/{ticket.id}/attachments/"
        upload = owner_client.post(
            upload_url,
            {"file": SimpleUploadedFile("details.txt", b"private details", content_type="text/plain")},
            format="multipart",
        )
        self.assertEqual(upload.status_code, 201)
        attachment = TicketAttachment.objects.get(id=upload.data["id"])
        self.addCleanup(attachment.file.delete, save=False)

        download_url = f"{upload_url}{attachment.id}/"
        download = owner_client.get(download_url)
        self.assertEqual(download.status_code, 200)
        b"".join(download.streaming_content)
        download.close()
        self.assertEqual(self.client_as(self.other).get(download_url).status_code, 403)

        invalid_type = owner_client.post(
            upload_url,
            {"file": SimpleUploadedFile("script.svg", b"<svg/>", content_type="image/svg+xml")},
            format="multipart",
        )
        self.assertEqual(invalid_type.status_code, 400)
        oversized = owner_client.post(
            upload_url,
            {
                "file": SimpleUploadedFile(
                    "large.txt",
                    b"x" * (10 * 1024 * 1024 + 1),
                    content_type="text/plain",
                )
            },
            format="multipart",
        )
        self.assertEqual(oversized.status_code, 400)

    def test_status_assignment_history_and_audit_are_idempotent(self):
        ticket = self.create_ticket()
        support_client = self.client_as(self.support)

        changed = support_client.post(
            f"/api/admin/support/tickets/{ticket.id}/status/",
            {"status": Ticket.STATUS_IN_PROGRESS, "reason": "Picked up"},
            format="json",
        )
        self.assertEqual(changed.status_code, 200)
        invalid_transition = support_client.post(
            f"/api/admin/support/tickets/{ticket.id}/status/",
            {"status": Ticket.STATUS_IN_PROGRESS},
            format="json",
        )
        self.assertEqual(invalid_transition.status_code, 400)

        assignment_url = f"/api/admin/support/tickets/{ticket.id}/assignment/"
        first_assignment = support_client.post(
            assignment_url,
            {"assignee_id": str(self.support.user_id)},
            format="json",
        )
        repeated_assignment = support_client.post(
            assignment_url,
            {"assignee_id": str(self.support.user_id)},
            format="json",
        )
        self.assertEqual(first_assignment.status_code, 200)
        self.assertEqual(repeated_assignment.status_code, 200)
        self.assertEqual(TicketStatusHistory.objects.filter(ticket=ticket).count(), 1)
        self.assertEqual(TicketAssignmentHistory.objects.filter(ticket=ticket).count(), 1)
        self.assertEqual(AdminAuditEvent.objects.filter(action="support_status_changed").count(), 1)
        self.assertEqual(AdminAuditEvent.objects.filter(action="support_ticket_assigned").count(), 1)
        detail = support_client.get(f"/api/admin/support/tickets/{ticket.id}/")
        self.assertEqual(len(detail.data["status_history"]), 1)
        self.assertEqual(len(detail.data["assignment_history"]), 1)

    def test_user_delete_archives_ticket_and_records_transition(self):
        ticket = self.create_ticket()
        response = self.client_as(self.owner).delete(f"/api/support/tickets/{ticket.id}/")

        self.assertEqual(response.status_code, 204)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_CLOSED)
        self.assertTrue(Ticket.objects.filter(id=ticket.id).exists())
        self.assertEqual(TicketStatusHistory.objects.filter(ticket=ticket).count(), 1)

    def test_user_reply_reopens_waiting_or_closed_ticket_once(self):
        ticket = self.create_ticket()
        support_client = self.client_as(self.support)
        owner_client = self.client_as(self.owner)
        support_client.post(
            f"/api/admin/support/tickets/{ticket.id}/messages/",
            {"text": "Подскажите подробнее"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="ask-for-details",
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_WAITING_FOR_USER)

        message_url = f"/api/support/tickets/{ticket.id}/messages/"
        reply = owner_client.post(
            message_url,
            {"text": "Вот детали"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="user-details",
        )
        repeated_reply = owner_client.post(
            message_url,
            {"text": "Вот детали"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="user-details",
        )
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(repeated_reply.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_WAITING_FOR_SUPPORT)
        self.assertEqual(TicketMessage.objects.filter(ticket=ticket).count(), 2)
        self.assertEqual(TicketStatusHistory.objects.filter(ticket=ticket).count(), 2)
