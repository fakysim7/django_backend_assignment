import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from storage.models import StoredFile
from .storage import support_attachment_storage
from .categories import CATEGORY_CHOICES


class Ticket(models.Model):
    STATUS_NEW = "new"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_WAITING_FOR_USER = "waiting_for_user"
    STATUS_WAITING_FOR_SUPPORT = "waiting_for_support"
    STATUS_RESOLVED = "resolved"
    STATUS_CLOSED = "closed"

    STATUS_CHOICES = [
        (STATUS_NEW, "new"),
        (STATUS_IN_PROGRESS, "in_progress"),
        (STATUS_WAITING_FOR_USER, "waiting_for_user"),
        (STATUS_WAITING_FOR_SUPPORT, "waiting_for_support"),
        (STATUS_RESOLVED, "resolved"),
        (STATUS_CLOSED, "closed"),
    ]

    PRIORITY_LOW = "low"
    PRIORITY_NORMAL = "normal"
    PRIORITY_HIGH = "high"
    PRIORITY_URGENT = "urgent"

    PRIORITY_CHOICES = [
        (PRIORITY_LOW, "low"),
        (PRIORITY_NORMAL, "normal"),
        (PRIORITY_HIGH, "high"),
        (PRIORITY_URGENT, "urgent"),
    ]

    ALLOWED_TRANSITIONS = {
        STATUS_NEW: {
            STATUS_IN_PROGRESS,
            STATUS_WAITING_FOR_USER,
            STATUS_WAITING_FOR_SUPPORT,
            STATUS_RESOLVED,
            STATUS_CLOSED,
        },
        STATUS_IN_PROGRESS: {STATUS_WAITING_FOR_USER, STATUS_WAITING_FOR_SUPPORT, STATUS_RESOLVED, STATUS_CLOSED},
        STATUS_WAITING_FOR_USER: {STATUS_IN_PROGRESS, STATUS_WAITING_FOR_SUPPORT, STATUS_RESOLVED, STATUS_CLOSED},
        STATUS_WAITING_FOR_SUPPORT: {STATUS_IN_PROGRESS, STATUS_RESOLVED, STATUS_CLOSED},
        STATUS_RESOLVED: {STATUS_IN_PROGRESS, STATUS_WAITING_FOR_SUPPORT, STATUS_CLOSED},
        STATUS_CLOSED: {STATUS_NEW, STATUS_IN_PROGRESS, STATUS_WAITING_FOR_SUPPORT},
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="support_tickets",
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    category = models.CharField(
        max_length=64,
        choices=(("", "Не указана"),) + CATEGORY_CHOICES,
        blank=True,
        default="",
        db_index=True,
    )
    priority = models.CharField(
        max_length=16,
        choices=PRIORITY_CHOICES,
        default=PRIORITY_NORMAL,
    )
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_NEW,
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_support_tickets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-updated_at", "-created_at")

    def __str__(self):
        return f"{self.title} ({self.status})"

    def update_status(self, new_status, actor=None, reason=""):
        if new_status not in self.ALLOWED_TRANSITIONS.get(self.status, set()):
            raise ValidationError(
                f"Status transition from '{self.status}' to '{new_status}' is not allowed."
            )

        previous_status = self.status
        self.status = new_status
        if new_status == self.STATUS_RESOLVED:
            self.resolved_at = timezone.now()
        elif previous_status == self.STATUS_RESOLVED and new_status != self.STATUS_RESOLVED:
            self.resolved_at = None

        if new_status == self.STATUS_CLOSED:
            self.closed_at = timezone.now()
        elif previous_status == self.STATUS_CLOSED and new_status != self.STATUS_CLOSED:
            self.closed_at = None

        self.save(update_fields=["status", "resolved_at", "closed_at", "updated_at"])
        return TicketStatusHistory.objects.create(
            ticket=self,
            previous_status=previous_status,
            new_status=new_status,
            actor=actor,
            reason=reason,
        )

    def assign_user(self, new_assignee, actor=None, reason=""):
        if self.assignee == new_assignee:
            return self

        previous_assignee = self.assignee
        self.assignee = new_assignee
        self.save(update_fields=["assignee", "updated_at"])
        return TicketAssignmentHistory.objects.create(
            ticket=self,
            previous_assignee=previous_assignee,
            new_assignee=new_assignee,
            actor=actor,
            reason=reason,
        )
        return self


class TicketMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(Ticket, related_name="messages", on_delete=models.CASCADE)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="support_messages",
    )
    text = models.TextField()
    is_internal = models.BooleanField(default=False)
    idempotency_key = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("author", "idempotency_key"),
                condition=~models.Q(idempotency_key=""),
                name="support_message_author_idempotency_unique",
            )
        ]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.is_internal:
            self.ticket.last_message_at = self.created_at
            self.ticket.save(update_fields=["last_message_at", "updated_at"])


class TicketAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(Ticket, related_name="attachments", on_delete=models.CASCADE)
    message = models.ForeignKey(
        TicketMessage,
        related_name="attachments",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    file = models.FileField(
        upload_to="support_attachments/%Y/%m/%d",
        storage=support_attachment_storage,
        null=True,
        blank=True,
    )
    stored_file = models.ForeignKey(
        StoredFile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ticket_attachments",
    )
    filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=128, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("uploaded_at",)


class TicketStatusHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(Ticket, related_name="status_history", on_delete=models.CASCADE)
    previous_status = models.CharField(max_length=32, blank=True)
    new_status = models.CharField(max_length=32)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_status_history",
    )
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)


class TicketAssignmentHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(Ticket, related_name="assignment_history", on_delete=models.CASCADE)
    previous_assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_previous_assignments",
    )
    new_assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_new_assignments",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_assignment_history",
    )
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
