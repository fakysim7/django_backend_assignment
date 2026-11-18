import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import OperationalError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import Notification, OutboxEvent

MAX_ATTEMPTS = 5
LOCK_TIMEOUT = timedelta(minutes=5)
logger = logging.getLogger(__name__)


def _claim_event():
    now = timezone.now()
    stale_before = now - LOCK_TIMEOUT
    try:
        with transaction.atomic():
            event = (
                OutboxEvent.objects.select_for_update(skip_locked=True)
                .filter(
                    Q(
                        status=OutboxEvent.STATUS_PENDING,
                        next_attempt_at__isnull=True,
                    )
                    | Q(
                        status=OutboxEvent.STATUS_PENDING,
                        next_attempt_at__lte=now,
                    )
                    | Q(
                        status=OutboxEvent.STATUS_PROCESSING,
                        processing_started_at__lt=stale_before,
                    )
                )
                .order_by("created_at")
                .first()
            )
            if event is None:
                return None
            if event.attempts >= MAX_ATTEMPTS:
                event.status = OutboxEvent.STATUS_FAILED
                event.processing_started_at = None
                event.next_attempt_at = None
                event.last_error = "Maximum processing attempts exceeded."
                event.save(
                    update_fields=(
                        "status",
                        "processing_started_at",
                        "next_attempt_at",
                        "last_error",
                    )
                )
                return None
            claimed = OutboxEvent.objects.filter(
                pk=event.pk,
                status=event.status,
                attempts=event.attempts,
            ).update(
                status=OutboxEvent.STATUS_PROCESSING,
                attempts=event.attempts + 1,
                processing_started_at=now,
                next_attempt_at=None,
            )
            if not claimed:
                return None
            return event.pk
    except OperationalError as error:
        if any(word in str(error).lower() for word in ("locked", "deadlock", "lock")):
            logger.info("Notification outbox claim deferred due to concurrent worker.")
            return None
        raise


def _deliver(event_id):
    with transaction.atomic():
        event = OutboxEvent.objects.select_for_update().get(pk=event_id)
        if event.status != OutboxEvent.STATUS_PROCESSING:
            return
        recipient_id = event.payload["recipient_id"]
        recipient = get_user_model().objects.get(
            pk=recipient_id,
            is_active=True,
        )
        metadata = event.payload["metadata"]
        if not isinstance(metadata, dict):
            raise ValueError("Notification metadata must be an object.")
        Notification.objects.get_or_create(
            outbox_event=event,
            defaults={
                "recipient": recipient,
                "type": event.event_type,
                "title": event.payload["title"],
                "message": event.payload["message"],
                "payload": metadata,
                "route": event.payload.get("route", ""),
            },
        )
        event.status = OutboxEvent.STATUS_PROCESSED
        event.processed_at = timezone.now()
        event.processing_started_at = None
        event.last_error = ""
        event.save(
            update_fields=(
                "status",
                "processed_at",
                "processing_started_at",
                "last_error",
            )
        )


def _record_failure(event_id, error):
    now = timezone.now()
    with transaction.atomic():
        event = OutboxEvent.objects.select_for_update().get(pk=event_id)
        if event.status != OutboxEvent.STATUS_PROCESSING:
            return
        event.last_error = f"{type(error).__name__}: notification processing failed"[:255]
        event.processing_started_at = None
        if event.attempts >= MAX_ATTEMPTS:
            event.status = OutboxEvent.STATUS_FAILED
            event.next_attempt_at = None
        else:
            event.status = OutboxEvent.STATUS_PENDING
            event.next_attempt_at = now + timedelta(
                seconds=min(60 * (2 ** (event.attempts - 1)), 3600)
            )
        event.save(
            update_fields=(
                "status",
                "next_attempt_at",
                "processing_started_at",
                "last_error",
            )
        )


def run_worker_once():
    event_id = _claim_event()
    if event_id is None:
        return False
    try:
        _deliver(event_id)
    except Exception as error:
        _record_failure(event_id, error)
    return True
