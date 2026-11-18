from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from storage.models import StoredFile
from storage.providers import StorageError
from storage.registry import get_provider
from storage.services import _is_linked_to_project


class Command(BaseCommand):
    help = "Clean old, unlinked pending/failed/deleted storage objects."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-hours", type=int, required=True)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete unlinked objects. Without this flag the command is dry-run.",
        )

    def handle(self, *args, **options):
        hours = options["older_than_hours"]
        if hours < 1:
            raise CommandError("--older-than-hours must be greater than zero.")
        cutoff = timezone.now() - timedelta(hours=hours)
        candidates = StoredFile.objects.filter(
            status__in=(
                StoredFile.STATUS_PENDING,
                StoredFile.STATUS_FAILED,
                StoredFile.STATUS_DELETING,
                StoredFile.STATUS_DELETED,
            ),
            updated_at__lt=cutoff,
        ).order_by("updated_at")
        deleted = 0
        skipped = 0
        for stored_file in candidates.iterator():
            if stored_file.ticket_attachments.exists() or _is_linked_to_project(stored_file):
                skipped += 1
                self.stdout.write(f"{stored_file.id}: linked; skipped")
                continue
            if not options["apply"]:
                self.stdout.write(f"{stored_file.id}: would clean")
                continue
            try:
                provider = get_provider(stored_file.provider)
                if provider.exists(stored_file.storage_key):
                    provider.delete(stored_file.storage_key)
            except StorageError as error:
                self.stderr.write(
                    self.style.ERROR(
                        f"{stored_file.id}: {type(error).__name__}; cleanup failed"
                    )
                )
                continue
            stored_file.status = StoredFile.STATUS_DELETED
            stored_file.deleted_at = stored_file.deleted_at or timezone.now()
            stored_file.save(
                update_fields=("status", "deleted_at", "updated_at")
            )
            deleted += 1
        mode = "Cleaned" if options["apply"] else "Dry run"
        self.stdout.write(f"{mode}: {deleted} cleaned, {skipped} linked file(s) skipped.")
