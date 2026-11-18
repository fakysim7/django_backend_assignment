from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models
import uuid


class PlatformAdminAccount(AbstractBaseUser):
    username = models.CharField(max_length=150, unique=True)
    rbac_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="platform_admin_account",
    )
    is_active = models.BooleanField(default=True)
    session_generation = models.UUIDField(default=uuid.uuid4, editable=False)

    USERNAME_FIELD = "username"

    def save(self, *args, **kwargs):
        self.username = self.username.strip().casefold()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.username


class AdminAuditEvent(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="platform_admin_actions",
    )
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="platform_admin_role_events",
    )
    action = models.CharField(max_length=64)
    before_roles = models.JSONField(default=list)
    after_roles = models.JSONField(default=list)
    reason = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        default_permissions = ()
        permissions = (
            ("view_admin_profile", "Can view the current administrative profile"),
            ("view_admin_dashboard", "Can view the administrative dashboard"),
            ("manage_admin_roles", "Can manage platform role assignments"),
            ("view_admin_audit", "Can view platform administration audit events"),
            ("view_support_queue", "Can access the support queue"),
            ("view_support_tickets", "Can view individual support tickets"),
            ("manage_support_tickets", "Can manage support ticket lifecycle"),
            ("reply_support_tickets", "Can reply to support tickets"),
            ("assign_support_tickets", "Can assign support tickets"),
            ("add_internal_support_notes", "Can add internal support notes"),
            ("moderate_user_content", "Can moderate user content"),
            ("view_platform_analytics", "Can view platform analytics"),
        )


class PlatformBootstrapState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    admin = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="platform_admin_bootstrap",
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
