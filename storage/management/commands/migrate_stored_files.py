from django.core.management.base import BaseCommand, CommandError

from storage.models import StoredFile
from storage.providers import StorageError
from storage.services import migrate_stored_file


class Command(BaseCommand):
    help = "Migrate a bounded batch of StoredFile objects to another provider."

    def add_arguments(self, parser):
        parser.add_argument("--provider", required=True)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--file-id")
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Perform migration; without this flag only list selected files.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1:
            raise CommandError("--limit must be greater than zero.")
        files = StoredFile.objects.filter(
            status=StoredFile.STATUS_UPLOADED
        ).exclude(provider=options["provider"])
        if options["file_id"]:
            files = files.filter(id=options["file_id"])
        files = list(files.order_by("created_at")[:limit])

        if not options["execute"]:
            self.stdout.write(
                f"Dry run: {len(files)} uploaded file(s) would be migrated."
            )
            for stored_file in files:
                self.stdout.write(f"{stored_file.id} ({stored_file.provider})")
            return

        failed = 0
        for stored_file in files:
            try:
                migrate_stored_file(stored_file, options["provider"])
            except (StorageError, ValueError) as error:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(
                        f"{stored_file.id}: {type(error).__name__}; migration failed"
                    )
                )
            else:
                self.stdout.write(self.style.SUCCESS(f"{stored_file.id}: migrated"))
        if failed:
            raise CommandError(f"{failed} file migration(s) failed.")
