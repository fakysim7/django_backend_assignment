import hashlib
import tempfile
import uuid

from django.conf import settings
from django.core.files.base import File
from django.db import transaction
from django.utils import timezone

from .models import StoredFile, StorageMigration
from .providers import StorageError
from .registry import get_provider
from .validation import validate_signature, validate_upload_descriptor

MAX_MIGRATION_ATTEMPTS = 3


def _is_linked_to_project(stored_file):
    if stored_file.category != "document_images":
        return False
    from main.models import Projects

    for project in Projects.objects.filter(user=stored_file.owner).only("blocks"):
        if any(
            isinstance(block.get("content"), dict)
            and block["content"].get("storage_file_id") == str(stored_file.id)
            for block in (project.blocks or [])
            if isinstance(block, dict)
        ):
            return True
    return False


def _key_for(file_id, filename):
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    if not extension.isalnum() or len(extension) > 10:
        extension = "bin"
    return f"objects/{file_id.hex[:2]}/{file_id.hex}.{extension}"


def initialize_upload(
    *,
    owner,
    filename,
    content_type,
    size,
    category="other",
    visibility=StoredFile.VISIBILITY_PRIVATE,
    metadata=None,
):
    normalized, _ = validate_upload_descriptor(filename, content_type, size)
    if not category or len(category) > 64:
        raise ValueError("Invalid file category.")
    if visibility not in dict(StoredFile.VISIBILITY_CHOICES):
        raise ValueError("Invalid file visibility.")
    provider = get_provider()
    file_id = uuid.uuid4()
    storage_key = _key_for(file_id, normalized)
    return StoredFile.objects.create(
        id=file_id,
        original_filename=str(filename)[:255],
        normalized_filename=normalized,
        mime_type=content_type,
        file_size=size,
        category=category,
        visibility=visibility,
        provider=provider.name,
        bucket=getattr(provider, "bucket", ""),
        storage_key=storage_key,
        owner=owner,
        metadata=metadata or {},
    )


def upload_pending_file(stored_file, content):
    with transaction.atomic():
        stored_file = StoredFile.objects.select_for_update().get(pk=stored_file.pk)
        if stored_file.status != StoredFile.STATUS_PENDING:
            if stored_file.status == StoredFile.STATUS_UPLOADED:
                return stored_file
            raise ValueError("File is not awaiting upload.")
        if getattr(content, "size", stored_file.file_size) != stored_file.file_size:
            raise ValueError("Uploaded file size does not match the initialized size.")
        position = content.tell()
        prefix = content.read(8192)
        validate_signature(prefix, stored_file.normalized_filename, stored_file.mime_type)
        content.seek(position)
        provider = get_provider(stored_file.provider)
        result = provider.upload(
            stored_file.storage_key,
            content,
            content_type=stored_file.mime_type,
            metadata={"stored-file-id": str(stored_file.id)},
        )
        if result.size != stored_file.file_size:
            provider.delete(stored_file.storage_key)
            raise ValueError("Uploaded file size does not match the initialized size.")
        stored_file.checksum = result.checksum
        stored_file.status = StoredFile.STATUS_UPLOADED
        stored_file.uploaded_at = timezone.now()
        stored_file.save(
            update_fields=("checksum", "status", "uploaded_at", "updated_at")
        )
        return stored_file


def confirm_upload(stored_file):
    with transaction.atomic():
        stored_file = StoredFile.objects.select_for_update().get(pk=stored_file.pk)
        if stored_file.status == StoredFile.STATUS_UPLOADED:
            return stored_file
        if stored_file.status != StoredFile.STATUS_PENDING:
            raise ValueError("File is not awaiting confirmation.")
        provider = get_provider(stored_file.provider)
        if not provider.exists(stored_file.storage_key):
            raise FileNotFoundError("Uploaded object does not exist.")
        metadata = provider.get_metadata(stored_file.storage_key)
        if metadata["size"] != stored_file.file_size:
            raise ValueError("Uploaded file size does not match initialized size.")
        digest = hashlib.sha256()
        prefix = b""
        with provider.download(stored_file.storage_key) as content:
            while True:
                chunk = content.read(1024 * 1024)
                if not chunk:
                    break
                if len(prefix) < 8192:
                    prefix += chunk[: 8192 - len(prefix)]
                digest.update(chunk)
        validate_signature(prefix, stored_file.normalized_filename, stored_file.mime_type)
        stored_file.checksum = digest.hexdigest()
        stored_file.status = StoredFile.STATUS_UPLOADED
        stored_file.uploaded_at = timezone.now()
        stored_file.save(
            update_fields=("checksum", "status", "uploaded_at", "updated_at")
        )
        return stored_file


def download_stored_file(stored_file):
    if stored_file.status != StoredFile.STATUS_UPLOADED:
        raise FileNotFoundError("Stored file is unavailable.")
    return get_provider(stored_file.provider).download(stored_file.storage_key)


def delete_stored_file(stored_file):
    if stored_file.status == StoredFile.STATUS_DELETED:
        return stored_file
    if stored_file.ticket_attachments.exists():
        raise ValueError("File is still linked to a business object.")
    if _is_linked_to_project(stored_file):
        raise ValueError("File is still linked to a business object.")
    stored_file.status = StoredFile.STATUS_DELETING
    stored_file.save(update_fields=("status", "updated_at"))
    get_provider(stored_file.provider).delete(stored_file.storage_key)
    stored_file.status = StoredFile.STATUS_DELETED
    stored_file.deleted_at = timezone.now()
    stored_file.save(update_fields=("status", "deleted_at", "updated_at"))
    return stored_file


def generate_upload_url(stored_file):
    return get_provider(stored_file.provider).generate_signed_upload_url(
        stored_file.storage_key,
        content_type=stored_file.mime_type,
        expires_in=settings.STORAGE_SIGNED_URL_TTL,
        content_length=stored_file.file_size,
    )


def generate_download_url(stored_file):
    return get_provider(stored_file.provider).generate_signed_download_url(
        stored_file.storage_key,
        expires_in=settings.STORAGE_SIGNED_URL_TTL,
        filename=stored_file.normalized_filename,
    )


def calculate_checksum(stored_file):
    return get_provider(stored_file.provider).checksum(stored_file.storage_key)


def migrate_stored_file(stored_file, target_provider_name):
    if stored_file.status != StoredFile.STATUS_UPLOADED:
        raise ValueError("Only uploaded files can be migrated.")
    source = get_provider(stored_file.provider)
    target = get_provider(target_provider_name)
    if source.name == target.name and stored_file.bucket == getattr(target, "bucket", ""):
        return None

    target_key = stored_file.storage_key
    migration, _ = StorageMigration.objects.get_or_create(
        stored_file=stored_file,
        source_provider=stored_file.provider,
        source_bucket=stored_file.bucket,
        source_key=stored_file.storage_key,
        target_provider=target.name,
        target_bucket=getattr(target, "bucket", ""),
        target_key=target_key,
        defaults={"status": StorageMigration.STATUS_PENDING},
    )
    if migration.status == StorageMigration.STATUS_COMPLETED:
        return migration
    if migration.status == StorageMigration.STATUS_FAILED:
        migration.status = StorageMigration.STATUS_PENDING
        migration.last_error = ""
        migration.save(update_fields=("status", "last_error"))

    source_checksum = stored_file.checksum
    last_error = None
    for _ in range(MAX_MIGRATION_ATTEMPTS):
        migration.attempts += 1
        migration.save(update_fields=("attempts",))
        try:
            if not source_checksum:
                source_checksum = source.checksum(stored_file.storage_key)
            with source.download(stored_file.storage_key) as content:
                digest = hashlib.sha256()
                with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
                    size = 0
                    for chunk in iter(lambda: content.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        spool.write(chunk)
                    if size != stored_file.file_size or digest.hexdigest() != source_checksum:
                        raise StorageError("Source file checksum verification failed.")
                    spool.seek(0)
                    result = target.upload(
                        target_key,
                        File(spool, name=stored_file.normalized_filename),
                        content_type=stored_file.mime_type,
                        metadata={"stored-file-id": str(stored_file.id)},
                    )
                    if (
                        result.size != stored_file.file_size
                        or result.checksum != source_checksum
                        or not target.exists(target_key)
                        or target.checksum(target_key) != source_checksum
                    ):
                        raise StorageError("Target file verification failed.")
            with transaction.atomic():
                stored_file.provider = target.name
                stored_file.bucket = getattr(target, "bucket", "")
                stored_file.storage_key = target_key
                stored_file.checksum = source_checksum
                stored_file.save(
                    update_fields=("provider", "bucket", "storage_key", "checksum", "updated_at")
                )
                migration.status = StorageMigration.STATUS_COMPLETED
                migration.checksum = source_checksum
                migration.completed_at = timezone.now()
                migration.last_error = ""
                migration.save(
                    update_fields=("status", "checksum", "completed_at", "last_error")
                )
            return migration
        except (StorageError, ValueError) as error:
            last_error = error

    migration.status = StorageMigration.STATUS_FAILED
    migration.last_error = f"{type(last_error).__name__}: migration failed"[:255]
    migration.save(update_fields=("status", "last_error"))
    raise StorageError("File migration failed after limited retries.") from last_error


def storage_diagnostics():
    from django.db.models import Count, Sum

    from .registry import provider_status

    status = provider_status()
    provider = get_provider() if status["configuration_ready"] else None
    available = None
    if provider:
        try:
            available = provider.check_bucket()
        except StorageError:
            available = False
    summary = StoredFile.objects.aggregate(
        count=Count("id"),
        total_size=Sum("file_size"),
    )
    categories = list(
        StoredFile.objects.values("category")
        .annotate(count=Count("id"), total_size=Sum("file_size"))
        .order_by("category")
    )
    return {
        **status,
        "available": available,
        "file_count": summary["count"] or 0,
        "total_size": summary["total_size"] or 0,
        "by_category": categories,
        "pending_count": StoredFile.objects.filter(status=StoredFile.STATUS_PENDING).count(),
        "failed_count": StoredFile.objects.filter(status=StoredFile.STATUS_FAILED).count(),
    }
