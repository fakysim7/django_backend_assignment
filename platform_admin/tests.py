import io
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    AdminAuditEvent,
    PlatformAdminAccount,
    PlatformBootstrapState,
)
from .roles import ROLE_GROUP_NAMES

User = get_user_model()


class PlatformAdminAPITests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = self.create_user("admin")
        self.support = self.create_user("support")
        self.moderator = self.create_user("moderator")
        self.analyst = self.create_user("analyst")
        self.regular_user = self.create_user("regular")
        self.assign_role(self.admin, "Admin")
        self.assign_role(self.support, "Support")
        self.assign_role(self.moderator, "Moderator")
        self.assign_role(self.analyst, "Analyst")

    def create_user(self, username):
        return User.objects.create_user(
            username=username,
            user_name=username,
            email=f"{username}@example.test",
            password="not-a-secret",
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
        account.set_password("DedicatedAdminPass123!")
        account.save()
        client.get("/api/admin/auth/csrf/")
        csrf_token = client.cookies["csrftoken"].value
        client.credentials(HTTP_X_CSRFTOKEN=csrf_token)
        response = client.post(
            "/api/admin/auth/login/",
            {"username": account.username, "password": "DedicatedAdminPass123!"},
            format="json",
        )
        if response.status_code != 200:
            raise AssertionError("Could not establish test admin session.")
        return client

    def test_admin_can_read_profile_roles_and_audit(self):
        client = self.client_as(self.admin)

        profile = client.get("/api/admin/me/")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.data["roles"], ["Admin"])
        self.assertEqual(
            [section["key"] for section in profile.data["sections"]],
            ["dashboard", "role_management", "support"],
        )

        roles = client.get("/api/admin/roles/")
        self.assertEqual(roles.status_code, 200)
        self.assertEqual(
            [role["name"] for role in roles.data],
            ["Admin", "Support", "Moderator", "Analyst"],
        )
        self.assertEqual(
            client.get("/api/admin/audit-events/").status_code,
            200,
        )

    def test_regular_and_staff_only_users_are_denied(self):
        self.regular_user.is_staff = True
        self.regular_user.save(update_fields=("is_staff",))
        client = self.client_as(self.regular_user)

        self.assertEqual(client.get("/api/admin/me/").status_code, 403)
        self.assertEqual(client.get("/api/admin/roles/").status_code, 403)
        self.assertEqual(
            client.get(f"/api/admin/users/{self.support.user_id}/roles/").status_code,
            403,
        )
        self.assertEqual(client.get("/api/admin/audit-events/").status_code, 403)
        self.assertEqual(
            client.put(
                f"/api/admin/users/{self.support.user_id}/roles/",
                {"roles": ["Admin"], "reason": "self elevation"},
                format="json",
            ).status_code,
            403,
        )

    def test_regular_user_jwt_does_not_authenticate_admin_apis(self):
        client = APIClient()
        access = str(RefreshToken.for_user(self.admin).access_token)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        self.assertEqual(client.get("/api/admin/me/").status_code, 401)
        self.assertEqual(client.get("/api/admin/support/tickets/").status_code, 401)
        self.assertEqual(
            client.post(
                "/api/ai/admin/grants/",
                {"user_id": str(self.regular_user.user_id), "amount": 1},
                format="json",
            ).status_code,
            401,
        )
        self.assertEqual(
            client.get("/api/storage/admin/diagnostics/").status_code,
            401,
        )

    def test_admin_login_logout_and_session_expiry(self):
        account = PlatformAdminAccount(
            username="separate-admin",
            rbac_user=self.admin,
        )
        account.set_password("DedicatedAdminPass123!")
        account.save()
        client = APIClient()

        self.assertEqual(client.get("/api/admin/me/").status_code, 401)
        self.assertEqual(
            client.post(
                "/api/admin/auth/login/",
                {"username": account.username, "password": "wrong-password"},
                format="json",
            ).status_code,
            401,
        )

        client.get("/api/admin/auth/csrf/")
        csrf_token = client.cookies["csrftoken"].value
        client.credentials(HTTP_X_CSRFTOKEN=csrf_token)
        login = client.post(
            "/api/admin/auth/login/",
            {"username": account.username, "password": "DedicatedAdminPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertTrue(client.cookies["finepapers_admin_session"]["httponly"])
        self.assertNotIn("access", login.data)
        self.assertEqual(client.get("/api/admin/me/").status_code, 200)

        logout = client.post("/api/admin/auth/logout/", {}, format="json")
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(client.get("/api/admin/me/").status_code, 401)

        login = client.post(
            "/api/admin/auth/login/",
            {"username": account.username, "password": "DedicatedAdminPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        from django.contrib.sessions.backends.db import SessionStore

        session_key = client.cookies["finepapers_admin_session"].value
        session = SessionStore(session_key=session_key)
        session.set_expiry(-1)
        session.save()
        self.assertEqual(client.get("/api/admin/me/").status_code, 401)

    def test_admin_login_is_rate_limited(self):
        cache.clear()
        client = APIClient()
        responses = [
            client.post(
                "/api/admin/auth/login/",
                {"username": "not-an-admin", "password": "wrong"},
                format="json",
            )
            for _ in range(6)
        ]
        cache.clear()
        self.assertEqual(
            [response.status_code for response in responses],
            [401, 401, 401, 401, 401, 429],
        )

    def test_admin_login_enforces_csrf(self):
        account = PlatformAdminAccount(
            username="csrf-admin",
            rbac_user=self.admin,
        )
        account.set_password("DedicatedAdminPass123!")
        account.save()
        client = APIClient(enforce_csrf_checks=True)
        csrf = client.get("/api/admin/auth/csrf/")
        self.assertEqual(csrf.status_code, 200)

        payload = {
            "username": account.username,
            "password": "DedicatedAdminPass123!",
        }
        rejected = client.post(
            "/api/admin/auth/login/",
            payload,
            format="json",
        )
        self.assertEqual(rejected.status_code, 403)

        accepted = client.post(
            "/api/admin/auth/login/",
            payload,
            format="json",
            HTTP_X_CSRFTOKEN=csrf.data["csrfToken"],
        )
        self.assertEqual(accepted.status_code, 200)

    def test_admin_account_bootstrap_and_password_rotation(self):
        create_command = (
            "platform_admin.management.commands.create_platform_admin.getpass.getpass"
        )
        with patch(
            create_command,
            side_effect=["Violet!HarborQuartz947", "Violet!HarborQuartz947"],
        ):
            call_command(
                "create_platform_admin",
                username="panel-login",
                rbac_user=self.admin.user_name,
                stdout=io.StringIO(),
            )
        account = PlatformAdminAccount.objects.get(username="panel-login")
        self.assertTrue(account.check_password("Violet!HarborQuartz947"))
        self.assertFalse(account.check_password("not-a-secret"))

        original_generation = account.session_generation
        rotate_command = (
            "platform_admin.management.commands.set_platform_admin_password.getpass.getpass"
        )
        with patch(
            rotate_command,
            side_effect=["Orbit!CobaltWindow583", "Orbit!CobaltWindow583"],
        ):
            call_command(
                "set_platform_admin_password",
                username="panel-login",
                stdout=io.StringIO(),
            )
        account.refresh_from_db()
        self.assertTrue(account.check_password("Orbit!CobaltWindow583"))
        self.assertNotEqual(account.session_generation, original_generation)

    def test_public_registration_cannot_set_staff_superuser_or_roles(self):
        response = APIClient().post(
            "/api/register/",
            {
                "user_name": "self-promote",
                "email": "self-promote@example.test",
                "password": "SafePass123!",
                "is_staff": True,
                "is_superuser": True,
                "groups": [ROLE_GROUP_NAMES["Admin"]],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        user = User.objects.get(user_name="self-promote")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.groups.exists())

    def test_role_permissions_are_limited_to_their_scopes(self):
        support_profile = self.client_as(self.support).get("/api/admin/me/")
        moderator_profile = self.client_as(self.moderator).get("/api/admin/me/")
        analyst_profile = self.client_as(self.analyst).get("/api/admin/me/")

        self.assertEqual(support_profile.status_code, 200)
        self.assertEqual(support_profile.data["roles"], ["Support"])
        self.assertIn("platform_admin.view_support_queue", support_profile.data["permissions"])
        self.assertIn(
            "platform_admin.view_support_tickets",
            support_profile.data["permissions"],
        )
        self.assertIn(
            "platform_admin.reply_support_tickets",
            support_profile.data["permissions"],
        )
        self.assertNotIn(
            "platform_admin.manage_admin_roles",
            support_profile.data["permissions"],
        )
        self.assertEqual(
            [section["key"] for section in support_profile.data["sections"]],
            ["support"],
        )

        self.assertEqual(moderator_profile.status_code, 200)
        self.assertIn(
            "platform_admin.moderate_user_content",
            moderator_profile.data["permissions"],
        )
        self.assertNotIn(
            "platform_admin.view_admin_dashboard",
            moderator_profile.data["permissions"],
        )
        self.assertEqual(moderator_profile.data["sections"], [])

        self.assertEqual(analyst_profile.status_code, 200)
        self.assertIn(
            "platform_admin.view_admin_dashboard",
            analyst_profile.data["permissions"],
        )
        self.assertNotIn(
            "platform_admin.manage_admin_roles",
            analyst_profile.data["permissions"],
        )
        self.assertEqual(
            [section["key"] for section in analyst_profile.data["sections"]],
            ["dashboard"],
        )

        for user in (self.support, self.moderator, self.analyst):
            self.assertEqual(
                self.client_as(user).get("/api/admin/roles/").status_code,
                403,
            )

    def test_admin_can_assign_another_user_and_change_is_audited(self):
        client = self.client_as(self.admin)
        endpoint = f"/api/admin/users/{self.regular_user.user_id}/roles/"

        response = client.put(
            endpoint,
            {"roles": ["Support", "Analyst"], "reason": "On-call and reporting duties"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["roles"], ["Support", "Analyst"])
        event = AdminAuditEvent.objects.get(subject=self.regular_user)
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.before_roles, [])
        self.assertEqual(event.after_roles, ["Analyst", "Support"])
        self.assertEqual(event.reason, "On-call and reporting duties")

        repeated_response = client.put(
            endpoint,
            {"roles": ["Support", "Analyst"], "reason": "No additional change"},
            format="json",
        )
        self.assertEqual(repeated_response.status_code, 200)
        self.assertEqual(AdminAuditEvent.objects.filter(subject=self.regular_user).count(), 1)

        audit_response = client.get("/api/admin/audit-events/")
        self.assertEqual(audit_response.status_code, 200)
        self.assertEqual(audit_response.data["count"], 1)

    def test_existing_ai_grants_accept_platform_admin_and_legacy_staff_only(self):
        endpoint = "/api/ai/admin/grants/"
        payload = {
            "user_id": str(self.regular_user.user_id),
            "amount": 10,
            "reason": "Support adjustment",
        }

        with patch("ai_pipeline.views.grant_tokens", return_value=10):
            platform_admin_response = self.client_as(self.admin).post(
                endpoint,
                payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="grant-platform-admin",
            )
            self.assertEqual(platform_admin_response.status_code, 200)

            legacy_staff = self.create_user("legacy-staff")
            legacy_staff.is_staff = True
            legacy_staff.save(update_fields=("is_staff",))
            staff_response = self.client_as(legacy_staff).post(
                endpoint,
                payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="grant-legacy-staff",
            )
            self.assertEqual(staff_response.status_code, 200)

        regular_response = self.client_as(self.regular_user).post(
            endpoint,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="grant-regular-user",
        )
        self.assertEqual(regular_response.status_code, 403)

    def test_admin_cannot_change_own_roles(self):
        response = self.client_as(self.admin).put(
            f"/api/admin/users/{self.admin.user_id}/roles/",
            {"roles": ["Admin", "Analyst"], "reason": "self elevation"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AdminAuditEvent.objects.exists())

    def test_invalid_role_assignments_are_rejected(self):
        client = self.client_as(self.admin)
        endpoint = f"/api/admin/users/{self.regular_user.user_id}/roles/"

        invalid_role = client.put(
            endpoint,
            {"roles": ["Owner"], "reason": "invalid role"},
            format="json",
        )
        self.assertEqual(invalid_role.status_code, 400)
        self.assertFalse(AdminAuditEvent.objects.exists())

        self.assertEqual(
            client.put(
                f"/api/admin/users/{self.admin.user_id}/roles/",
                {"roles": [], "reason": "self removal"},
                format="json",
            ).status_code,
            403,
        )

    def test_role_assignment_can_remove_another_admin_without_removing_actor(self):
        second_admin = self.create_user("second-admin")
        self.assign_role(second_admin, "Admin")
        response = self.client_as(self.admin).put(
            f"/api/admin/users/{second_admin.user_id}/roles/",
            {"roles": [], "reason": "Access no longer required"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["roles"], [])
        self.assertTrue(
            self.admin.groups.filter(name=ROLE_GROUP_NAMES["Admin"]).exists()
        )

    def test_role_assignment_requires_reason_and_keeps_non_platform_groups(self):
        unrelated_group = Group.objects.create(name="Other application")
        self.regular_user.groups.add(unrelated_group)
        client = self.client_as(self.admin)

        missing_reason = client.put(
            f"/api/admin/users/{self.regular_user.user_id}/roles/",
            {"roles": ["Moderator"]},
            format="json",
        )
        self.assertEqual(missing_reason.status_code, 400)

        assigned = client.put(
            f"/api/admin/users/{self.regular_user.user_id}/roles/",
            {"roles": ["Moderator"], "reason": "Content moderation"},
            format="json",
        )
        self.assertEqual(assigned.status_code, 200)
        self.assertTrue(self.regular_user.groups.filter(pk=unrelated_group.pk).exists())

    def test_missing_user_and_anonymous_requests_are_denied(self):
        client = APIClient()
        self.assertIn(client.get("/api/admin/me/").status_code, (401, 403))

        response = self.client_as(self.admin).get(
            f"/api/admin/users/{uuid4()}/roles/"
        )
        self.assertEqual(response.status_code, 404)


class AdminBootstrapCommandTests(TestCase):
    def test_bootstrap_assigns_first_admin_without_password_output(self):
        user = User.objects.create_user(
            username="first-admin",
            user_name="first-admin",
            email="first-admin@example.test",
            password="not-a-secret",
        )
        output = io.StringIO()

        call_command("bootstrap_admin", username=user.user_name, stdout=output)

        user.refresh_from_db()
        state = PlatformBootstrapState.objects.get(pk=1)
        self.assertTrue(
            user.groups.filter(name=ROLE_GROUP_NAMES["Admin"]).exists()
        )
        self.assertEqual(state.admin, user)
        self.assertIsNotNone(state.completed_at)
        self.assertNotIn("not-a-secret", output.getvalue())
        audit_event = AdminAuditEvent.objects.get(subject=user)
        self.assertIsNone(audit_event.actor)
        self.assertEqual(audit_event.after_roles, ["Admin"])
        audit_response = APIClient()
        audit_response.force_authenticate(user=user)
        self.assertEqual(
            audit_response.get("/api/admin/audit-events/").data["results"][0]["actor_id"],
            None,
        )

    def test_bootstrap_refuses_second_run_or_missing_account(self):
        user = User.objects.create_user(
            username="first-admin",
            user_name="first-admin",
            email="first-admin@example.test",
            password="not-a-secret",
        )
        call_command("bootstrap_admin", username=user.user_name, stdout=io.StringIO())

        with self.assertRaises(CommandError):
            call_command("bootstrap_admin", username=user.user_name, stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command("bootstrap_admin", username="unknown", stdout=io.StringIO())

    def test_bootstrap_refuses_existing_superuser(self):
        User.objects.create_superuser(
            username="existing-superuser",
            user_name="existing-superuser",
            email="existing@example.test",
            password="not-a-secret",
        )
        user = User.objects.create_user(
            username="candidate",
            user_name="candidate",
            email="candidate@example.test",
            password="not-a-secret",
        )

        with self.assertRaises(CommandError):
            call_command("bootstrap_admin", username=user.user_name, stdout=io.StringIO())

        self.assertFalse(
            user.groups.filter(name=ROLE_GROUP_NAMES["Admin"]).exists()
        )
