from django.contrib.auth import get_user_model
from django.db.models import Q

from platform_admin.roles import ROLE_GROUP_NAMES
from notifications.services import enqueue_in_app_notification

User = get_user_model()
STAFF_GROUPS = (ROLE_GROUP_NAMES["Admin"], ROLE_GROUP_NAMES["Support"])


def _support_staff():
    return User.objects.filter(
        Q(groups__name__in=STAFF_GROUPS) | Q(is_superuser=True),
        is_active=True,
    ).distinct()


def _enqueue(
    recipient,
    *,
    event_type,
    idempotency_key,
    title,
    message,
    route,
    ticket,
):
    return enqueue_in_app_notification(
        recipient_id=recipient.pk,
        event_type=event_type,
        idempotency_key=f"{idempotency_key}:{recipient.user_id}",
        title=title,
        message=message,
        route=route,
        payload={"ticket_id": str(ticket.id)},
    )


def notify_new_ticket(ticket):
    for recipient in _support_staff().exclude(pk=ticket.user_id):
        _enqueue(
            recipient,
            event_type="support.ticket.created",
            idempotency_key=f"support.ticket.created:{ticket.id}",
            title="Новое обращение",
            message="Поступило новое обращение в поддержку.",
            route=f"/admin/support/{ticket.id}",
            ticket=ticket,
        )


def notify_ticket_message(ticket, message):
    if message.is_internal:
        return

    if message.author_id == ticket.user_id:
        assignee = (
            User.objects.filter(pk=ticket.assignee_id, is_active=True).first()
            if ticket.assignee_id
            else None
        )
        recipients = (
            User.objects.filter(pk=assignee.pk)
            if assignee
            else _support_staff()
        ).exclude(pk=message.author_id)
        event_type = "support.ticket.user_message"
        title = "Новое сообщение в обращении"
        body = "Пользователь отправил новое сообщение в обращении."
        route = f"/admin/support/{ticket.id}"
    else:
        recipients = User.objects.filter(pk=ticket.user_id, is_active=True).exclude(
            pk=message.author_id
        )
        event_type = "support.ticket.public_reply"
        title = "Ответ поддержки"
        body = "Сотрудник поддержки ответил на ваше обращение."
        route = "/support"

    for recipient in recipients:
        _enqueue(
            recipient,
            event_type=event_type,
            idempotency_key=f"{event_type}:{message.id}",
            title=title,
            message=body,
            route=route,
            ticket=ticket,
        )


def notify_ticket_status(ticket, history):
    if history.actor_id == ticket.user_id:
        return
    status_labels = {
        ticket.STATUS_NEW: "новый",
        ticket.STATUS_IN_PROGRESS: "в работе",
        ticket.STATUS_WAITING_FOR_USER: "ожидает вашего ответа",
        ticket.STATUS_WAITING_FOR_SUPPORT: "ожидает ответа поддержки",
        ticket.STATUS_RESOLVED: "решён",
        ticket.STATUS_CLOSED: "закрыт",
    }
    if history.new_status == ticket.STATUS_RESOLVED:
        event_type = "support.ticket.resolved"
        title = "Обращение решено"
        body = "Сотрудник поддержки отметил обращение как решённое."
    elif history.new_status == ticket.STATUS_CLOSED:
        event_type = "support.ticket.closed"
        title = "Обращение закрыто"
        body = "Сотрудник поддержки закрыл обращение."
    else:
        event_type = "support.ticket.status_changed"
        title = "Изменился статус обращения"
        body = f"Новый статус обращения: {status_labels[history.new_status]}."

    recipient = User.objects.filter(pk=ticket.user_id, is_active=True).first()
    if recipient:
        _enqueue(
            recipient,
            event_type=event_type,
            idempotency_key=f"{event_type}:{history.id}",
            title=title,
            message=body,
            route="/support",
            ticket=ticket,
        )


def notify_ticket_assignment(ticket, history):
    assignee = history.new_assignee
    if not assignee or not assignee.is_active or history.actor_id == assignee.pk:
        return
    _enqueue(
        assignee,
        event_type="support.ticket.assigned",
        idempotency_key=f"support.ticket.assigned:{history.id}",
        title="Назначено обращение",
        message="Вам назначено обращение в поддержку.",
        route=f"/admin/support/{ticket.id}",
        ticket=ticket,
    )
