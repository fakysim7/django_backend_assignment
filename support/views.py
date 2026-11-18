from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    PermissionDenied,
    ValidationError as DRFValidationError,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from platform_admin.models import AdminAuditEvent
from platform_admin.authentication import PlatformAdminAPIView
from platform_admin.roles import ROLE_GROUP_NAMES
from storage.models import StoredFile
from storage.providers import StorageError
from storage.services import download_stored_file, initialize_upload, upload_pending_file
from storage.validation import validate_signature

from .notification_events import (
    notify_new_ticket,
    notify_ticket_assignment,
    notify_ticket_message,
    notify_ticket_status,
)
from .categories import (
    CATEGORY_CODES,
    CATEGORY_UNSPECIFIED,
    CATEGORY_UNSPECIFIED_FILTER,
    SUPPORT_CATEGORIES,
)
from .models import (
    Ticket,
    TicketAssignmentHistory,
    TicketAttachment,
    TicketMessage,
    TicketStatusHistory,
)
from .serializers import (
    SupportAdminTicketSerializer,
    SupportQueueTicketSerializer,
    TicketAttachmentSerializer,
    TicketCreateSerializer,
    TicketMessageSerializer,
    TicketSerializer,
    UserTicketSerializer,
)

User = get_user_model()
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024


class IdempotencyConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Этот Idempotency-Key уже использован для другого действия."


class SupportCategoryListView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        return Response(SUPPORT_CATEGORIES)


def _has_platform_permission(user, codename):
    return user.is_superuser or user.has_perm(f"platform_admin.{codename}")


def _ticket_queryset():
    return Ticket.objects.select_related("user", "assignee").prefetch_related(
        Prefetch(
            "messages",
            queryset=TicketMessage.objects.select_related("author"),
        ),
        Prefetch(
            "attachments",
            queryset=TicketAttachment.objects.select_related("message"),
        ),
    )


def _admin_ticket_queryset():
    return _ticket_queryset().prefetch_related(
        Prefetch(
            "status_history",
            queryset=TicketStatusHistory.objects.select_related("actor"),
        ),
        Prefetch(
            "assignment_history",
            queryset=TicketAssignmentHistory.objects.select_related(
                "actor", "previous_assignee", "new_assignee"
            ),
        ),
    )


def _record_support_audit(actor, ticket, action, reason):
    AdminAuditEvent.objects.create(
        actor=actor,
        subject=ticket.user,
        action=action,
        reason=f"ticket:{ticket.id}; {reason}"[:500],
    )


def _create_message(request, ticket, *, is_internal):
    text = request.data.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return Response(
            {"detail": "Текст сообщения обязателен."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if len(idempotency_key) > 128:
        return Response(
            {"detail": "Idempotency-Key не должен превышать 128 символов."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    defaults = {
        "ticket": ticket,
        "text": text.strip(),
        "is_internal": is_internal,
    }
    with transaction.atomic():
        status_history = None
        if idempotency_key:
            message, created = TicketMessage.objects.get_or_create(
                author=request.user,
                idempotency_key=idempotency_key,
                defaults=defaults,
            )
            if not created and (
                message.ticket_id != ticket.id
                or message.text != defaults["text"]
                or message.is_internal != is_internal
            ):
                raise IdempotencyConflict()
        else:
            message = TicketMessage.objects.create(
                author=request.user,
                idempotency_key="",
                **defaults,
            )
            created = True

        if created and not is_internal:
            previous_status = ticket.status
            if (
                ticket.user_id == request.user.id
                and ticket.status in {
                    Ticket.STATUS_WAITING_FOR_USER,
                    Ticket.STATUS_RESOLVED,
                    Ticket.STATUS_CLOSED,
                }
            ):
                status_history = ticket.update_status(
                    Ticket.STATUS_WAITING_FOR_SUPPORT,
                    actor=request.user,
                    reason="User public reply",
                )
            elif ticket.user_id != request.user.id and ticket.status in {
                Ticket.STATUS_NEW,
                Ticket.STATUS_IN_PROGRESS,
                Ticket.STATUS_WAITING_FOR_SUPPORT,
            }:
                status_history = ticket.update_status(
                    Ticket.STATUS_WAITING_FOR_USER,
                    actor=request.user,
                    reason="Support public reply",
                )
            if (
                ticket.user_id != request.user.id
                and previous_status != ticket.status
            ):
                _record_support_audit(
                    request.user,
                    ticket,
                    "support_status_changed",
                    f"from:{previous_status}; to:{ticket.status}",
                )
        elif created:
            _record_support_audit(
                request.user,
                ticket,
                "support_internal_note_added",
                "Internal note added",
            )
        if created and not is_internal:
            if status_history:
                notify_ticket_status(ticket, status_history)
            notify_ticket_message(ticket, message)

    response_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return Response(TicketMessageSerializer(message).data, status=response_status)


class TicketListCreateView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        tickets = _ticket_queryset().filter(user=request.user)
        return Response(UserTicketSerializer(tickets, many=True).data)

    def post(self, request):
        serializer = TicketCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            ticket = Ticket.objects.create(
                user=request.user,
                title=serializer.validated_data["title"],
                description=serializer.validated_data["description"],
                category=serializer.validated_data["category"],
                priority=serializer.validated_data.get(
                    "priority", Ticket.PRIORITY_NORMAL
                ),
                status=Ticket.STATUS_NEW,
            )
            notify_new_ticket(ticket)
        return Response(
            UserTicketSerializer(ticket).data,
            status=status.HTTP_201_CREATED,
        )


class TicketDetailView(APIView):
    permission_classes = (IsAuthenticated,)

    def get_object(self, request, ticket_id):
        ticket = get_object_or_404(_ticket_queryset(), id=ticket_id)
        if ticket.user_id != request.user.id:
            raise PermissionDenied("У вас нет доступа к этому тикету.")
        return ticket

    def get(self, request, ticket_id):
        ticket = self.get_object(request, ticket_id)
        return Response(UserTicketSerializer(ticket).data)

    def patch(self, request, ticket_id):
        ticket = self.get_object(request, ticket_id)
        if "status" in request.data and not _has_platform_permission(
            request.user, "manage_support_tickets"
        ):
            raise PermissionDenied("Изменять статус можно только через управление поддержкой.")
        if "status" in request.data:
            return Response(
                {"detail": "Используйте endpoint смены статуса поддержки."},
                status=400,
            )
        if "category" in request.data:
            return Response(
                {"detail": "Категорию нельзя изменить после создания обращения."},
                status=400,
            )
        if "title" in request.data:
            title = request.data["title"]
            if not isinstance(title, str) or not title.strip() or len(title) > 255:
                return Response({"detail": "Недопустимая тема обращения."}, status=400)
            ticket.title = title.strip()
        if "description" in request.data:
            description = request.data["description"]
            if not isinstance(description, str) or not description.strip():
                return Response({"detail": "Описание обращения обязательно."}, status=400)
            ticket.description = description.strip()
        ticket.save(update_fields=["title", "description", "updated_at"])
        return Response(UserTicketSerializer(ticket).data)

    def delete(self, request, ticket_id):
        ticket = self.get_object(request, ticket_id)
        if ticket.status != Ticket.STATUS_CLOSED:
            try:
                with transaction.atomic():
                    history = ticket.update_status(
                        Ticket.STATUS_CLOSED,
                        actor=request.user,
                        reason="Closed by ticket owner",
                    )
                    notify_ticket_status(ticket, history)
            except ValidationError as exc:
                return Response({"detail": str(exc)}, status=400)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TicketMessageCreateView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, ticket_id):
        ticket = get_object_or_404(Ticket, id=ticket_id, user=request.user)
        return _create_message(request, ticket, is_internal=False)


class SupportTicketStatusView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, ticket_id):
        if not _has_platform_permission(request.user, "manage_support_tickets"):
            raise PermissionDenied("Недостаточно прав для смены статуса.")

        new_status = request.data.get("status")
        reason = request.data.get("reason", "")
        if new_status not in dict(Ticket.STATUS_CHOICES):
            return Response({"detail": "Недопустимый статус."}, status=400)
        if not isinstance(reason, str) or len(reason) > 255:
            return Response({"detail": "Причина не должна превышать 255 символов."}, status=400)

        with transaction.atomic():
            ticket = get_object_or_404(
                Ticket.objects.select_for_update().select_related("user", "assignee"),
                id=ticket_id,
            )
            previous_status = ticket.status
            try:
                history = ticket.update_status(
                    new_status,
                    actor=request.user,
                    reason=reason.strip(),
                )
            except ValidationError as exc:
                return Response({"detail": str(exc)}, status=400)
            _record_support_audit(
                request.user,
                ticket,
                "support_status_changed",
                f"from:{previous_status}; to:{new_status}",
            )
            notify_ticket_status(ticket, history)

        return Response(TicketSerializer(ticket).data)


class SupportTicketAssignmentView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, ticket_id):
        if not _has_platform_permission(request.user, "assign_support_tickets"):
            raise PermissionDenied("Недостаточно прав для назначения.")

        assignee_id = request.data.get("assignee_id")
        if assignee_id in (None, ""):
            assignee = None
        else:
            try:
                assignee_id = UUID(str(assignee_id))
            except (TypeError, ValueError):
                return Response({"detail": "Некорректный ID исполнителя."}, status=400)
            assignee = get_object_or_404(
                User.objects.filter(
                    Q(groups__name__in=(ROLE_GROUP_NAMES["Admin"], ROLE_GROUP_NAMES["Support"]))
                    | Q(is_superuser=True)
                ).filter(is_active=True).distinct(),
                user_id=assignee_id,
            )

        reason = request.data.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 255:
            return Response({"detail": "Причина не должна превышать 255 символов."}, status=400)

        with transaction.atomic():
            ticket = get_object_or_404(
                Ticket.objects.select_for_update().select_related("user", "assignee"),
                id=ticket_id,
            )
            previous_assignee = ticket.assignee
            history = ticket.assign_user(
                assignee,
                actor=request.user,
                reason=reason.strip(),
            )
            if previous_assignee != ticket.assignee:
                previous_id = (
                    str(previous_assignee.user_id) if previous_assignee else "none"
                )
                new_id = str(ticket.assignee.user_id) if ticket.assignee else "none"
                _record_support_audit(
                    request.user,
                    ticket,
                    "support_ticket_assigned",
                    f"from:{previous_id}; to:{new_id}",
                )
                notify_ticket_assignment(ticket, history)
        return Response(TicketSerializer(ticket).data)


class SupportTicketNoteView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, ticket_id):
        if not _has_platform_permission(request.user, "add_internal_support_notes"):
            raise PermissionDenied("Нет прав на внутренние заметки.")
        ticket = get_object_or_404(Ticket, id=ticket_id)
        return _create_message(request, ticket, is_internal=True)


class SupportPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response(
            {
                "count": self.page.paginator.count,
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "summary": self.summary,
                "results": data,
            }
        )


class SupportAdminTicketListView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        if not _has_platform_permission(request.user, "view_support_queue"):
            raise PermissionDenied("Нет доступа к очереди поддержки.")

        all_tickets = Ticket.objects.all()
        summary = {
            "new_count": all_tickets.filter(status=Ticket.STATUS_NEW).count(),
            "waiting_for_support_count": all_tickets.filter(
                status=Ticket.STATUS_WAITING_FOR_SUPPORT
            ).count(),
        }
        tickets = _ticket_queryset()
        search = request.query_params.get("search", "").strip()
        if search:
            filters = Q(title__icontains=search) | Q(
                user__user_name__icontains=search
            ) | Q(user__email__icontains=search)
            try:
                filters |= Q(id=UUID(search))
            except ValueError:
                pass
            tickets = tickets.filter(filters)

        status_filter = request.query_params.get("status")
        if status_filter:
            if status_filter not in dict(Ticket.STATUS_CHOICES):
                return Response({"detail": "Недопустимый статус."}, status=400)
            tickets = tickets.filter(status=status_filter)

        category = request.query_params.get("category", "").strip()
        if category == CATEGORY_UNSPECIFIED_FILTER:
            tickets = tickets.filter(category=CATEGORY_UNSPECIFIED)
        elif category:
            if category not in CATEGORY_CODES:
                return Response({"detail": "Недопустимая категория."}, status=400)
            tickets = tickets.filter(category=category)

        assignee_id = request.query_params.get("assignee")
        if assignee_id == "unassigned":
            tickets = tickets.filter(assignee__isnull=True)
        elif assignee_id:
            try:
                assignee_id = UUID(assignee_id)
            except ValueError:
                return Response({"detail": "Некорректный ID исполнителя."}, status=400)
            tickets = tickets.filter(assignee__user_id=assignee_id)

        ordering = request.query_params.get("ordering", "-last_message_at")
        if ordering not in {
            "created_at",
            "-created_at",
            "last_message_at",
            "-last_message_at",
        }:
            return Response({"detail": "Недопустимая сортировка."}, status=400)
        tickets = tickets.order_by(ordering, "-updated_at", "-created_at")

        category_counts = {category["code"]: 0 for category in SUPPORT_CATEGORIES}
        unspecified_category_count = 0
        for row in tickets.order_by().values("category").annotate(count=Count("id")):
            category_code = row["category"]
            if category_code in category_counts:
                category_counts[category_code] = row["count"]
            else:
                unspecified_category_count += row["count"]

        paginator = SupportPagination()
        page = paginator.paginate_queryset(tickets, request, view=self)
        paginator.summary = {
            **summary,
            "category_counts": category_counts,
            "unspecified_category_count": unspecified_category_count,
        }
        serializer = SupportQueueTicketSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class SupportAdminTicketDetailView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, ticket_id):
        if not _has_platform_permission(request.user, "view_support_tickets"):
            raise PermissionDenied("Нет доступа к содержимому тикета поддержки.")
        ticket = get_object_or_404(_admin_ticket_queryset(), id=ticket_id)
        return Response(SupportAdminTicketSerializer(ticket).data)


class SupportAssigneeListView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        if not _has_platform_permission(request.user, "assign_support_tickets"):
            raise PermissionDenied("Недостаточно прав для просмотра исполнителей.")
        users = User.objects.filter(
            Q(groups__name__in=(ROLE_GROUP_NAMES["Admin"], ROLE_GROUP_NAMES["Support"]))
            | Q(is_superuser=True)
        ).filter(is_active=True).distinct().order_by("user_name")
        return Response(
            [
                {"id": str(user.user_id), "user_name": user.user_name}
                for user in users
            ]
        )


class AdminTicketMessageCreateView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, ticket_id):
        if not _has_platform_permission(request.user, "reply_support_tickets"):
            raise PermissionDenied("Недостаточно прав для ответа пользователю.")
        ticket = get_object_or_404(Ticket, id=ticket_id)
        return _create_message(request, ticket, is_internal=False)


class SupportAttachmentUploadView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, ticket_id):
        ticket = get_object_or_404(Ticket, id=ticket_id)
        if ticket.user_id != request.user.id:
            raise PermissionDenied("Нет доступа к вложениям этого тикета.")

        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response({"detail": "Файл обязателен."}, status=400)
        if uploaded.size > min(MAX_ATTACHMENT_SIZE, settings.STORAGE_MAX_FILE_SIZE):
            return Response({"detail": "Размер файла не должен превышать 10 МБ."}, status=400)

        stored_file = None
        try:
            stored_file = initialize_upload(
                owner=request.user,
                filename=uploaded.name,
                content_type=getattr(uploaded, "content_type", "") or "",
                size=uploaded.size,
                category="support_attachments",
                metadata={"ticket_id": str(ticket.id)},
            )
            uploaded.seek(0)
            validate_signature(
                uploaded.read(8192),
                stored_file.normalized_filename,
                stored_file.mime_type,
            )
            uploaded.seek(0)
            stored_file = upload_pending_file(stored_file, uploaded)
            with transaction.atomic():
                attachment = TicketAttachment.objects.create(
                    ticket=ticket,
                    stored_file=stored_file,
                    filename=stored_file.normalized_filename,
                    mime_type=stored_file.mime_type,
                    size=stored_file.file_size,
                )
        except (ValidationError, DRFValidationError, ValueError) as exc:
            if stored_file:
                stored_file.status = StoredFile.STATUS_FAILED
                stored_file.save(update_fields=("status", "updated_at"))
            return Response({"detail": str(exc)}, status=400)
        except StorageError:
            return Response(
                {"detail": "Хранилище файлов временно недоступно."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            TicketAttachmentSerializer(attachment).data,
            status=status.HTTP_201_CREATED,
        )


def _attachment_file_response(attachment):
    content = (
        download_stored_file(attachment.stored_file)
        if attachment.stored_file_id
        else attachment.file.open("rb")
    )
    return FileResponse(
        content,
        as_attachment=True,
        filename=attachment.filename,
        content_type=attachment.mime_type or "application/octet-stream",
    )


class SupportAttachmentDownloadView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, ticket_id, attachment_id):
        ticket = get_object_or_404(Ticket, id=ticket_id)
        attachment = get_object_or_404(
            TicketAttachment.objects.select_related("message"),
            id=attachment_id,
            ticket=ticket,
        )
        if (
            ticket.user_id != request.user.id
            or (attachment.message_id and attachment.message.is_internal)
        ):
            raise PermissionDenied("Нет доступа к данному вложению.")
        return _attachment_file_response(attachment)


class AdminSupportAttachmentDownloadView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, ticket_id, attachment_id):
        if not _has_platform_permission(request.user, "view_support_tickets"):
            raise PermissionDenied("Нет доступа к содержимому тикета поддержки.")
        ticket = get_object_or_404(Ticket, id=ticket_id)
        attachment = get_object_or_404(
            TicketAttachment.objects.select_related("message"),
            id=attachment_id,
            ticket=ticket,
        )
        return _attachment_file_response(attachment)
