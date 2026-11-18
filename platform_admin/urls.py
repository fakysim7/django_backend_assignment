from django.urls import path

from .authentication import (
    AdminCsrfView,
    AdminLoginView,
    AdminLogoutView,
    AdminSessionView,
)
from .views import (
    AdminAuditEventsView,
    AdminProfileView,
    AdminRolesView,
    UserRoleAssignmentView,
)

urlpatterns = [
    path("auth/csrf/", AdminCsrfView.as_view(), name="admin-auth-csrf"),
    path("auth/login/", AdminLoginView.as_view(), name="admin-auth-login"),
    path("auth/session/", AdminSessionView.as_view(), name="admin-auth-session"),
    path("auth/logout/", AdminLogoutView.as_view(), name="admin-auth-logout"),
    path("me/", AdminProfileView.as_view(), name="admin-profile"),
    path("roles/", AdminRolesView.as_view(), name="admin-roles"),
    path(
        "users/<uuid:user_id>/roles/",
        UserRoleAssignmentView.as_view(),
        name="admin-user-roles",
    ),
    path(
        "audit-events/",
        AdminAuditEventsView.as_view(),
        name="admin-audit-events",
    ),
]
