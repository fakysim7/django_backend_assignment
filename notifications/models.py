import uuid

from django.conf import settings
from django.db import models


class OutboxEvent(models.Model):
    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_PROCESSED = "processed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_PROCESSED, "Processed"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=128)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=255, unique=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [
            models.Index(fields=("status", "next_attempt_at")),
            models.Index(fields=("status", "processing_started_at")),
        ]


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    type = models.CharField(max_length=128)
    title = models.CharField(max_length=255)
    message = models.TextField()
    payload = models.JSONField(default=dict)
    route = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    outbox_event = models.OneToOneField(
        OutboxEvent,
        on_delete=models.PROTECT,
        related_name="notification",
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("recipient", "-created_at")),
            models.Index(fields=("recipient", "read_at")),
        ]
