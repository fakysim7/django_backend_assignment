from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification
from .serializers import NotificationSerializer


class NotificationPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class NotificationListView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        notifications = Notification.objects.filter(recipient=request.user)
        read_filter = request.query_params.get("read")
        if read_filter in {"true", "1"}:
            notifications = notifications.filter(read_at__isnull=False)
        elif read_filter in {"false", "0"}:
            notifications = notifications.filter(read_at__isnull=True)
        elif read_filter not in (None, ""):
            return Response(
                {"detail": "Параметр read должен быть true или false."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ordering = request.query_params.get("ordering", "-created_at")
        if ordering not in {"created_at", "-created_at"}:
            return Response(
                {"detail": "Недопустимая сортировка."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        notifications = notifications.order_by(ordering, "id")
        paginator = NotificationPagination()
        page = paginator.paginate_queryset(notifications, request, view=self)
        return paginator.get_paginated_response(
            NotificationSerializer(page, many=True).data
        )


class NotificationUnreadCountView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        count = Notification.objects.filter(
            recipient=request.user,
            read_at__isnull=True,
        ).count()
        return Response({"count": count})


class NotificationReadView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, notification_id):
        updated = Notification.objects.filter(
            id=notification_id,
            recipient=request.user,
            read_at__isnull=True,
        ).update(read_at=timezone.now())
        if updated:
            notification = Notification.objects.get(
                id=notification_id,
                recipient=request.user,
            )
            return Response(NotificationSerializer(notification).data)

        notification = Notification.objects.filter(
            id=notification_id,
            recipient=request.user,
        ).first()
        if notification is None:
            return Response(
                {"detail": "Уведомление не найдено."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(NotificationSerializer(notification).data)


class NotificationReadAllView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        updated = Notification.objects.filter(
            recipient=request.user,
            read_at__isnull=True,
        ).update(read_at=timezone.now())
        return Response({"updated": updated})
