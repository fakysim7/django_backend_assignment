from django.db import migrations


def deactivate_smoke_owner(apps, schema_editor):
    User = apps.get_model("main", "User")
    User.objects.using(schema_editor.connection.alias).filter(
        user_name="storage-smoke-owner",
        email="storage-smoke-owner@example.invalid",
    ).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("main", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(deactivate_smoke_owner, migrations.RunPython.noop),
    ]
