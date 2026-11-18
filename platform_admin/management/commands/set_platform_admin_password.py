import getpass
import uuid

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from platform_admin.models import PlatformAdminAccount

class Command(BaseCommand):
    help = "Set a platform admin password and invalidate all existing admin sessions."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="Admin login name.")

    def handle(self, *args, **options):
        try:
            account = PlatformAdminAccount.objects.select_related("rbac_user").get(
                username__iexact=options["username"].strip()
            )
        except PlatformAdminAccount.DoesNotExist as exc:
            raise CommandError("Admin login not found.") from exc

        password = getpass.getpass("New admin password: ")
        confirmation = getpass.getpass("Confirm new admin password: ")
        if not password or password != confirmation:
            raise CommandError("Passwords are empty or do not match.")
        if account.rbac_user.check_password(password):
            raise CommandError("The admin password must differ from the user password.")
        try:
            password_validation.validate_password(password, user=account.rbac_user)
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc

        account.set_password(password)
        account.session_generation = uuid.uuid4()
        account.save(update_fields=("password", "session_generation"))
        self.stdout.write(self.style.SUCCESS("Admin password updated."))
