from types import SimpleNamespace
from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase


User = get_user_model()


class SmokeAccountMigrationTests(TestCase):
    def test_deactivates_only_the_exact_storage_smoke_account(self):
        smoke_user = User.objects.create_user(
            username="storage-smoke-owner",
            user_name="storage-smoke-owner",
            email="storage-smoke-owner@example.invalid",
            password="SmokeTestPass123!",
        )
        similar_user = User.objects.create_user(
            username="storage-smoke-owner2",
            user_name="storage-smoke-owner2",
            email="storage-smoke-owner2@example.invalid",
            password="SmokeTestPass123!",
        )
        migration = import_module(
            "main.migrations.0002_deactivate_storage_smoke_owner"
        )

        migration.deactivate_smoke_owner(
            apps,
            SimpleNamespace(connection=connection),
        )

        smoke_user.refresh_from_db()
        similar_user.refresh_from_db()
        self.assertFalse(smoke_user.is_active)
        self.assertTrue(similar_user.is_active)
