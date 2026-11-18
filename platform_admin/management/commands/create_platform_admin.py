import getpass

from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from platform_admin.models import PlatformAdminAccount
from platform_admin.roles import ROLE_GROUP_NAMES

User = get_user_model()


class Command(BaseCommand):
    help = "Create a separate admin login linked to an existing platform RBAC user."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="Admin login name.")
        parser.add_argument(
            "--rbac-user",
            required=True,
            help="Existing user's user_name whose platform roles and audit identity are used.",
        )

    def handle(self, *args, **options):
        username = options["username"].strip().casefold()
        try:
            rbac_user = User.objects.get(user_name=options["rbac_user"])
        except User.DoesNotExist as exc:
            raise CommandError("RBAC user not found.") from exc

        if not username:
            raise CommandError("Admin login name cannot be empty.")
        if not rbac_user.is_active:
            raise CommandError("Cannot create an admin login for an inactive user.")
        if not (
            rbac_user.is_superuser
            or rbac_user.groups.filter(name__in=ROLE_GROUP_NAMES.values()).exists()
        ):
            raise CommandError("The RBAC user must already have a platform role.")
        if PlatformAdminAccount.objects.filter(username__iexact=username).exists():
            raise CommandError("An admin login with this name already exists.")
        if PlatformAdminAccount.objects.filter(rbac_user=rbac_user).exists():
            raise CommandError("This RBAC user already has an admin login.")

        password = getpass.getpass("Admin password: ")
        confirmation = getpass.getpass("Confirm admin password: ")
        if not password or password != confirmation:
            raise CommandError("Passwords are empty or do not match.")
        if rbac_user.check_password(password):
            raise CommandError("The admin password must differ from the user password.")
        try:
            password_validation.validate_password(password, user=rbac_user)
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc

        account = PlatformAdminAccount(username=username, rbac_user=rbac_user)
        account.set_password(password)
        account.save()
        self.stdout.write(self.style.SUCCESS(f"Admin login created for {username}."))
