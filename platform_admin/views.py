from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AdminAuditEvent, PlatformBootstrapState
from .authentication import PlatformAdminAPIView
from .permissions import (
    CanManageAdminRoles,
    CanViewAdminAudit,
    CanViewAdminProfile,
)
from .roles import (
    ADMIN_SECTIONS,
    ALL_PLATFORM_PERMISSIONS,
    ROLE_GROUP_NAMES,
    ROLE_LABELS,
)
from .serializers import AdminAuditEventSerializer, RoleAssignmentSerializer

User = get_user_model()


def _platform_roles(user):
    group_names = set(
        user.groups.filter(name__in=ROLE_GROUP_NAMES.values())
        .values_list("name", flat=True)
    )
    roles = [
        role for role, group_name in ROLE_GROUP_NAMES.items()
        if group_name in group_names
    ]
    if user.is_superuser and "Admin" not in roles:
        roles.append("Admin")
    return roles


def _explicit_platform_roles(user):
    group_names = set(
        user.groups.filter(name__in=ROLE_GROUP_NAMES.values())
        .values_list("name", flat=True)
    )
    return [
        role for role, group_name in ROLE_GROUP_NAMES.items()
        if group_name in group_names
    ]


def _role_assignment_payload(user):
    return {
        "user": {
            "id": str(user.user_id),
            "user_name": user.user_name,
            "is_superuser": user.is_superuser,
        },
        "roles": _explicit_platform_roles(user),
    }


class AdminProfileView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated, CanViewAdminProfile)

    def get(self, request):
        permissions = [
            f"platform_admin.{codename}"
            for codename in ALL_PLATFORM_PERMISSIONS
            if request.user.has_perm(f"platform_admin.{codename}")
        ]
        sections = [
            {
                "key": section["key"],
                "label": section["label"],
                "path": section["path"],
            }
            for section in ADMIN_SECTIONS
            if request.user.has_perm(
                f"platform_admin.{section['permission']}"
            )
        ]
        return Response(
            {
                "user": {
                    "id": str(request.user.user_id),
                    "user_name": request.user.user_name,
                },
                "roles": _platform_roles(request.user),
                "permissions": permissions,
                "sections": sections,
            }
        )


class AdminRolesView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated, CanManageAdminRoles)

    def get(self, request):
        return Response(
            [
                {"name": name, "label": label}
                for name, label in ROLE_LABELS.items()
            ]
        )


class UserRoleAssignmentView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated, CanManageAdminRoles)

    def get_target(self, user_id):
        return get_object_or_404(User, user_id=user_id)

    def get(self, request, user_id):
        target = self.get_target(user_id)
        return Response(_role_assignment_payload(target))

    def put(self, request, user_id):
        target = self.get_target(user_id)
        if target.pk == request.user.pk:
            raise PermissionDenied("Нельзя изменять собственные административные роли.")

        serializer = RoleAssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_roles = set(serializer.validated_data["roles"])
        reason = serializer.validated_data["reason"]

        with transaction.atomic():
            PlatformBootstrapState.objects.select_for_update().get(pk=1)
            actor = User.objects.get(pk=request.user.pk)
            if not actor.is_superuser and not actor.has_perm(
                "platform_admin.manage_admin_roles"
            ):
                raise PermissionDenied(
                    "Недостаточно прав для управления административными ролями."
                )

            target = get_object_or_404(
                User.objects.select_for_update(),
                pk=target.pk,
            )
            existing_roles = set(_explicit_platform_roles(target))
            if requested_roles == existing_roles:
                return Response(_role_assignment_payload(target))

            if "Admin" in existing_roles and "Admin" not in requested_roles:
                other_admin_exists = (
                    User.objects.filter(is_superuser=True).exists()
                    or Group.objects.get(name=ROLE_GROUP_NAMES["Admin"])
                    .user_set.exclude(pk=target.pk)
                    .exists()
                )
                if not other_admin_exists:
                    raise ValidationError(
                        {"roles": "Нельзя снять роль последнего администратора."}
                    )

            groups_by_name = {
                group.name: group
                for group in Group.objects.filter(
                    name__in=ROLE_GROUP_NAMES.values()
                )
            }
            role_groups = {
                role: groups_by_name[group_name]
                for role, group_name in ROLE_GROUP_NAMES.items()
                if group_name in groups_by_name
            }
            missing_roles = requested_roles.difference(role_groups)
            if missing_roles:
                raise ValidationError(
                    {"roles": "Роли не настроены. Проверьте миграции платформы."}
                )

            for role in existing_roles - requested_roles:
                target.groups.remove(role_groups[role])
            for role in requested_roles - existing_roles:
                target.groups.add(role_groups[role])

            AdminAuditEvent.objects.create(
                actor=request.user,
                subject=target,
                action="roles_changed",
                before_roles=sorted(existing_roles),
                after_roles=sorted(requested_roles),
                reason=reason,
            )

        return Response(_role_assignment_payload(target))


class AdminAuditPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


class AdminAuditEventsView(ListAPIView):
    authentication_classes = PlatformAdminAPIView.authentication_classes
    permission_classes = (IsAuthenticated, CanViewAdminAudit)
    serializer_class = AdminAuditEventSerializer
    pagination_class = AdminAuditPagination
    queryset = AdminAuditEvent.objects.select_related("actor", "subject").all()
