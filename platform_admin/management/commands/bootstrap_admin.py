from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from platform_admin.models import AdminAuditEvent, PlatformBootstrapState
from platform_admin.roles import ROLE_GROUP_NAMES

User = get_user_model()


class Command(BaseCommand):
    help = "Assign the first platform Admin role to an existing account."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            required=True,
            help="Existing account's user_name; no password is requested or changed.",
        )

    def handle(self, *args, **options):
        username = options["username"]
        try:
            user = User.objects.get(user_name=username)
        except User.DoesNotExist as exc:
            raise CommandError(
                "Account not found. Register the account normally before bootstrapping."
            ) from exc

        if not user.is_active:
            raise CommandError("Cannot bootstrap an inactive account.")

        try:
            admin_group = Group.objects.get(name=ROLE_GROUP_NAMES["Admin"])
            state = PlatformBootstrapState.objects.get(pk=1)
        except (Group.DoesNotExist, PlatformBootstrapState.DoesNotExist) as exc:
            raise CommandError(
                "Platform role migrations are missing; run migrate first."
            ) from exc

        with transaction.atomic():
            state = PlatformBootstrapState.objects.select_for_update().get(pk=1)
            if state.completed_at is not None:
                raise CommandError("The initial platform administrator is already configured.")
            if (
                User.objects.filter(is_superuser=True).exists()
                or admin_group.user_set.exists()
            ):
                raise CommandError(
                    "An administrator already exists; bootstrap can only run once."
                )

            state.admin = user
            state.completed_at = timezone.now()
            state.save(update_fields=("admin", "completed_at"))
            user.groups.add(admin_group)
            AdminAuditEvent.objects.create(
                actor=None,
                subject=user,
                action="roles_changed",
                before_roles=[],
                after_roles=["Admin"],
                reason="Initial platform administrator bootstrap.",
            )

        self.stdout.write(
            self.style.SUCCESS(f"Platform Admin role assigned to {user.user_name}.")
        )
