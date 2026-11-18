from rest_framework.permissions import BasePermission


class HasPlatformPermission(BasePermission):
    permission_codename = ""

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (
                user.is_superuser
                or user.has_perm(
                    f"platform_admin.{self.permission_codename}"
                )
            )
        )


class CanViewAdminProfile(HasPlatformPermission):
    permission_codename = "view_admin_profile"


class CanManageAdminRoles(HasPlatformPermission):
    permission_codename = "manage_admin_roles"


class CanViewAdminAudit(HasPlatformPermission):
    permission_codename = "view_admin_audit"


class CanGrantPlatformTokens(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (
                user.is_staff
                or user.is_superuser
                or user.has_perm("platform_admin.manage_admin_roles")
            )
        )
