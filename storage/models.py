import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class StoredFile(models.Model):
    STATUS_PENDING = "pending"
    STATUS_UPLOADED = "uploaded"
    STATUS_FAILED = "failed"
    STATUS_DELETING = "deleting"
    STATUS_DELETED = "deleted"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_UPLOADED, "Uploaded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_DELETING, "Deleting"),
        (STATUS_DELETED, "Deleted"),
    ]

    VISIBILITY_PRIVATE = "private"
    VISIBILITY_PUBLIC = "public"
    VISIBILITY_CHOICES = [
        (VISIBILITY_PRIVATE, "Private"),
        (VISIBILITY_PUBLIC, "Public"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    original_filename = models.CharField(max_length=255)
    normalized_filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=128)
    file_size = models.PositiveBigIntegerField()
    checksum = models.CharField(max_length=64, blank=True)
    category = models.CharField(max_length=64, default="other", db_index=True)
    visibility = models.CharField(
        max_length=16,
        choices=VISIBILITY_CHOICES,
        default=VISIBILITY_PRIVATE,
    )
    provider = models.CharField(max_length=32)
    bucket = models.CharField(max_length=255, blank=True)
    storage_key = models.CharField(max_length=512, unique=True)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stored_files",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("owner", "status")),
            models.Index(fields=("provider", "bucket")),
        ]


class StorageMigration(models.Model):
    STATUS_PENDING = "pending"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stored_file = models.ForeignKey(
        StoredFile,
        on_delete=models.PROTECT,
        related_name="migrations",
    )
    source_provider = models.CharField(max_length=32)
    source_bucket = models.CharField(max_length=255, blank=True)
    source_key = models.CharField(max_length=512)
    target_provider = models.CharField(max_length=32)
    target_bucket = models.CharField(max_length=255, blank=True)
    target_key = models.CharField(max_length=512)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    checksum = models.CharField(max_length=64, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "stored_file",
                    "source_provider",
                    "source_bucket",
                    "source_key",
                    "target_provider",
                    "target_bucket",
                    "target_key",
                ),
                name="storage_migration_path_unique",
            )
        ]
