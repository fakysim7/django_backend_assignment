from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from .models import OutboxEvent

User = get_user_model()


def enqueue_in_app_notification(
    *,
    recipient_id,
    event_type,
    idempotency_key,
    title,
    message,
    route="",
    payload=None,
):
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Outbox events must be created inside a transaction.")

    if not event_type or len(event_type) > 128:
        raise ValueError("Notification event type is invalid.")
    if not idempotency_key or len(idempotency_key) > 255:
        raise ValueError("Notification idempotency key is invalid.")
    if not isinstance(title, str) or not title.strip() or len(title) > 255:
        raise ValueError("Notification title is invalid.")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Notification message is invalid.")
    if (
        not isinstance(route, str)
        or len(route) > 512
        or (route and (not route.startswith("/") or route.startswith("//")))
    ):
        raise ValueError("Notification route must be an internal path.")

    metadata = {} if payload is None else payload
    if not isinstance(metadata, dict):
        raise ValueError("Notification payload metadata must be an object.")
    if not User.objects.filter(pk=recipient_id, is_active=True).exists():
        return None

    event_payload = {
        "recipient_id": str(recipient_id),
        "title": title,
        "message": message,
        "route": route,
        "metadata": metadata,
    }
    try:
        event, created = OutboxEvent.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={"event_type": event_type, "payload": event_payload},
        )
    except IntegrityError:
        event = OutboxEvent.objects.get(idempotency_key=idempotency_key)
        created = False

    if not created and (
        event.event_type != event_type or event.payload != event_payload
    ):
        raise ValueError("Idempotency key was already used for a different event.")
    return event
