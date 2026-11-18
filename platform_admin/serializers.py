from rest_framework import serializers

from .roles import ROLE_LABELS


class RoleAssignmentSerializer(serializers.Serializer):
    roles = serializers.ListField(
        child=serializers.ChoiceField(choices=tuple(ROLE_LABELS)),
        allow_empty=True,
    )
    reason = serializers.CharField(
        max_length=500,
        allow_blank=False,
        trim_whitespace=True,
    )

    def validate_roles(self, roles):
        if len(roles) != len(set(roles)):
            raise serializers.ValidationError("Роли не должны повторяться.")
        return roles


class AdminAuditEventSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    actor_id = serializers.UUIDField(source="actor.user_id", allow_null=True)
    actor_name = serializers.CharField(source="actor.user_name", allow_null=True)
    subject_id = serializers.UUIDField(source="subject.user_id", allow_null=True)
    subject_name = serializers.CharField(source="subject.user_name", allow_null=True)
    action = serializers.CharField()
    before_roles = serializers.ListField(child=serializers.CharField())
    after_roles = serializers.ListField(child=serializers.CharField())
    reason = serializers.CharField()
    created_at = serializers.DateTimeField()
