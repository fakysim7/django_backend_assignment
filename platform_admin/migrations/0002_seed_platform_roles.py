from django.db import migrations


ROLE_PERMISSIONS = {
    "Admin": {
        "view_admin_profile",
        "view_admin_dashboard",
        "manage_admin_roles",
        "view_admin_audit",
        "view_support_queue",
        "moderate_user_content",
        "view_platform_analytics",
    },
    "Support": {
        "view_admin_profile",
        "view_support_queue",
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

PERMISSION_LABELS = {
    "view_admin_profile": "Can view the current administrative profile",
    "view_admin_dashboard": "Can view the administrative dashboard",
    "manage_admin_roles": "Can manage platform role assignments",
    "view_admin_audit": "Can view platform administration audit events",
    "view_support_queue": "Can access the support queue",
    "moderate_user_content": "Can moderate user content",
    "view_platform_analytics": "Can view platform analytics",
}


def create_platform_roles(apps, schema_editor):
    database = schema_editor.connection.alias
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    BootstrapState = apps.get_model("platform_admin", "PlatformBootstrapState")

    content_type, _ = ContentType.objects.using(database).get_or_create(
        app_label="platform_admin",
        model="adminauditevent",
    )
    permissions = {}
    for codename, label in PERMISSION_LABELS.items():
        permission, _ = Permission.objects.using(database).get_or_create(
            content_type_id=content_type.pk,
            codename=codename,
            defaults={"name": label},
        )
        permissions[codename] = permission

    for role, codenames in ROLE_PERMISSIONS.items():
        group, _ = Group.objects.using(database).get_or_create(
            name=f"Finepapers: {role}"
        )
        group.permissions.add(*(permissions[codename] for codename in codenames))

    BootstrapState.objects.using(database).get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("platform_admin", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_platform_roles, migrations.RunPython.noop),
    ]
