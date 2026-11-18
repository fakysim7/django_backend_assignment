from django.db import migrations


SUPPORT_PERMISSIONS = {
    "view_support_queue": "Can access the support queue",
    "view_support_tickets": "Can view individual support tickets",
    "manage_support_tickets": "Can manage support ticket lifecycle",
    "reply_support_tickets": "Can reply to support tickets",
    "assign_support_tickets": "Can assign support tickets",
    "add_internal_support_notes": "Can add internal support notes",
}


def grant_support_permissions(apps, schema_editor):
    database = schema_editor.connection.alias
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    content_type = ContentType.objects.using(database).get(
        app_label="platform_admin",
        model="adminauditevent",
    )
    permissions = {}
    for codename, label in SUPPORT_PERMISSIONS.items():
        permission, _ = Permission.objects.using(database).get_or_create(
            content_type_id=content_type.pk,
            codename=codename,
            defaults={"name": label},
        )
        permissions[codename] = permission

    admin = Group.objects.using(database).get(name="Finepapers: Admin")
    support = Group.objects.using(database).get(name="Finepapers: Support")
    admin.permissions.add(*permissions.values())
    support.permissions.add(*permissions.values())


class Migration(migrations.Migration):
    dependencies = [
        ("platform_admin", "0003_alter_adminauditevent_options"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(grant_support_permissions, migrations.RunPython.noop),
    ]
