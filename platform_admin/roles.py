ROLE_LABELS = {
    "Admin": "Администратор",
    "Support": "Поддержка",
    "Moderator": "Модератор",
    "Analyst": "Аналитик",
}

ROLE_GROUP_NAMES = {
    role: f"Finepapers: {role}"
    for role in ROLE_LABELS
}

ROLE_PERMISSIONS = {
    "Admin": {
        "view_admin_profile",
        "view_admin_dashboard",
        "manage_admin_roles",
        "view_admin_audit",
        "view_support_queue",
        "view_support_tickets",
        "manage_support_tickets",
        "reply_support_tickets",
        "assign_support_tickets",
        "add_internal_support_notes",
        "moderate_user_content",
        "view_platform_analytics",
    },
    "Support": {
        "view_admin_profile",
        "view_support_queue",
        "view_support_tickets",
        "manage_support_tickets",
        "reply_support_tickets",
        "assign_support_tickets",
        "add_internal_support_notes",
    },
    "Moderator": {
        "view_admin_profile",
        "moderate_user_content",
    },
    "Analyst": {
        "view_admin_profile",
        "view_admin_dashboard",
        "view_platform_analytics",
    },
}

ALL_PLATFORM_PERMISSIONS = sorted(
    {codename for permissions in ROLE_PERMISSIONS.values() for codename in permissions}
)

ADMIN_SECTIONS = (
    {
        "key": "dashboard",
        "label": "Обзор",
        "path": "/admin/",
        "permission": "view_admin_dashboard",
    },
    {
        "key": "role_management",
        "label": "Роли и доступ",
        "path": "/admin/roles/",
        "permission": "manage_admin_roles",
    },
    {
        "key": "support",
        "label": "Поддержка",
        "path": "/admin/support",
        "permission": "view_support_queue",
    },
)
