from rest_framework import serializers

from .categories import CATEGORY_CHOICES
from .models import Ticket, TicketAttachment, TicketMessage


def serialize_user_ref(user):
    if not user:
        return None
    return {"id": str(user.user_id), "user_name": user.user_name}


class UserRefSerializer(serializers.Serializer):
    id = serializers.SerializerMethodField()
    user_name = serializers.SerializerMethodField()

    def get_id(self, obj):
        return str(obj.user_id) if hasattr(obj, "user_id") else str(obj.id)

    def get_user_name(self, obj):
        return getattr(obj, "user_name", "")


class TicketSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    assignee = serializers.SerializerMethodField()
    messages = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()
    message_count = serializers.SerializerMethodField()

    class Meta:
        model = Ticket
        fields = (
            "id",
            "user",
            "title",
            "description",
            "category",
            "priority",
            "status",
            "assignee",
            "created_at",
            "updated_at",
            "last_message_at",
            "resolved_at",
            "closed_at",
            "message_count",
            "messages",
            "attachments",
        )
        read_only_fields = fields

    def _serialize_user(self, user):
        return serialize_user_ref(user)

    def get_user(self, obj):
        return self._serialize_user(obj.user)

    def get_assignee(self, obj):
        return self._serialize_user(obj.assignee)

    def get_messages(self, obj):
        messages = [message for message in obj.messages.all() if not message.is_internal]
        return [
            {
                "id": str(message.id),
                "author": self._serialize_user(message.author),
                "text": message.text,
                "is_internal": message.is_internal,
                "created_at": message.created_at.isoformat(),
            }
            for message in messages
        ]

    def get_attachments(self, obj):
        attachments = [
            attachment
            for attachment in obj.attachments.all()
            if not attachment.message_id or not attachment.message.is_internal
        ]
        return [
            {
                "id": str(attachment.id),
                "stored_file_id": (
                    str(attachment.stored_file_id) if attachment.stored_file_id else None
                ),
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "size": attachment.size,
                "uploaded_at": attachment.uploaded_at.isoformat(),
            }
            for attachment in attachments
        ]

    def get_message_count(self, obj):
        return sum(not message.is_internal for message in obj.messages.all())


class UserTicketSerializer(TicketSerializer):
    class Meta(TicketSerializer.Meta):
        fields = (
            "id",
            "title",
            "description",
            "category",
            "status",
            "created_at",
            "updated_at",
            "last_message_at",
            "message_count",
            "messages",
            "attachments",
        )

    def get_attachments(self, obj):
        attachments = [
            attachment
            for attachment in obj.attachments.all()
            if not attachment.message_id or not attachment.message.is_internal
        ]
        return [
            {
                "id": str(attachment.id),
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "size": attachment.size,
                "uploaded_at": attachment.uploaded_at.isoformat(),
            }
            for attachment in attachments
        ]


class SupportAdminTicketSerializer(TicketSerializer):
    status_history = serializers.SerializerMethodField()
    assignment_history = serializers.SerializerMethodField()

    class Meta(TicketSerializer.Meta):
        fields = TicketSerializer.Meta.fields + (
            "status_history",
            "assignment_history",
        )

    def get_messages(self, obj):
        return [
            {
                "id": str(message.id),
                "author": self._serialize_user(message.author),
                "text": message.text,
                "is_internal": message.is_internal,
                "created_at": message.created_at.isoformat(),
            }
            for message in obj.messages.all()
        ]

    def get_message_count(self, obj):
        return len(obj.messages.all())

    def get_attachments(self, obj):
        return [
            {
                "id": str(attachment.id),
                "stored_file_id": (
                    str(attachment.stored_file_id) if attachment.stored_file_id else None
                ),
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "size": attachment.size,
                "uploaded_at": attachment.uploaded_at.isoformat(),
            }
            for attachment in obj.attachments.all()
        ]

    def get_status_history(self, obj):
        return [
            {
                "id": str(event.id),
                "previous_status": event.previous_status,
                "new_status": event.new_status,
                "actor": self._serialize_user(event.actor),
                "reason": event.reason,
                "created_at": event.created_at.isoformat(),
            }
            for event in obj.status_history.all()
        ]

    def get_assignment_history(self, obj):
        return [
            {
                "id": str(event.id),
                "previous_assignee": self._serialize_user(event.previous_assignee),
                "new_assignee": self._serialize_user(event.new_assignee),
                "actor": self._serialize_user(event.actor),
                "reason": event.reason,
                "created_at": event.created_at.isoformat(),
            }
            for event in obj.assignment_history.all()
        ]


class SupportQueueTicketSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    assignee = serializers.SerializerMethodField()
    message_count = serializers.SerializerMethodField()

    class Meta:
        model = Ticket
        fields = (
            "id",
            "user",
            "title",
            "category",
            "priority",
            "status",
            "assignee",
            "created_at",
            "updated_at",
            "last_message_at",
            "message_count",
        )

    def get_user(self, obj):
        return serialize_user_ref(obj.user)

    def get_assignee(self, obj):
        return serialize_user_ref(obj.assignee)

    def get_message_count(self, obj):
        return sum(not message.is_internal for message in obj.messages.all())


class TicketCreateSerializer(serializers.ModelSerializer):
    category = serializers.ChoiceField(
        choices=CATEGORY_CHOICES,
        required=True,
        error_messages={
            "invalid_choice": "Выберите категорию из доступного списка."
        },
    )

    class Meta:
        model = Ticket
        fields = ("title", "description", "category", "priority")


class TicketMessageSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()

    class Meta:
        model = TicketMessage
        fields = ("id", "author", "text", "is_internal", "created_at")
        read_only_fields = fields

    def get_author(self, obj):
        return {"id": str(obj.author.user_id), "user_name": obj.author.user_name}


class TicketAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = TicketAttachment
        fields = ("id", "filename", "mime_type", "size", "uploaded_at")
        read_only_fields = fields
