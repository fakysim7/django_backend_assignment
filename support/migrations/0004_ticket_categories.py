from django.db import migrations, models


CATEGORY_CODES = {
    "generation",
    "editor",
    "export",
    "account",
    "pricing_tokens",
    "technical",
    "suggestion",
    "other",
}


def normalize_ticket_categories(apps, schema_editor):
    Ticket = apps.get_model("support", "Ticket")
    database = schema_editor.connection.alias
    Ticket.objects.using(database).exclude(category__in=CATEGORY_CODES).update(category="")


class Migration(migrations.Migration):

    dependencies = [
        ("support", "0003_ticketattachment_stored_file_and_more"),
    ]

    operations = [
        migrations.RunPython(normalize_ticket_categories, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="ticket",
            name="category",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Не указана"),
                    ("generation", "Генерация"),
                    ("editor", "Документы и редактор"),
                    ("export", "Экспорт и скачивание"),
                    ("account", "Аккаунт"),
                    ("pricing_tokens", "Тарифы и токены"),
                    ("technical", "Техническая проблема"),
                    ("suggestion", "Предложение"),
                    ("other", "Другое"),
                ],
                db_index=True,
                default="",
                max_length=64,
            ),
        ),
    ]
