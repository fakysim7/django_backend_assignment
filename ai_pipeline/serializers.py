from rest_framework import serializers

from .models import (
    AIOperation,
    AIProviderAttempt,
    AIPipeline,
    PipelineVersion,
    TokenLedgerEntry,
)


class AIProviderAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIProviderAttempt
        fields = (
            "attempt_number",
            "model_name",
            "status",
            "is_retryable",
            "input_tokens",
            "output_tokens",
            "provider_cost_usd",
            "error",
            "started_at",
            "completed_at",
        )


class AIOperationSerializer(serializers.ModelSerializer):
    provider_attempts = AIProviderAttemptSerializer(many=True, read_only=True)

    class Meta:
        model = AIOperation
        fields = (
            "id",
            "task_type",
            "status",
            "result",
            "error",
            "model_name",
            "input_tokens",
            "output_tokens",
            "provider_cost_usd",
            "reserved_tokens",
            "charged_tokens",
            "provider_attempts",
            "created_at",
            "started_at",
            "completed_at",
        )


class PipelineVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PipelineVersion
        fields = ("id", "stage", "version", "data", "created_at")


class AIPipelineSerializer(serializers.ModelSerializer):
    operations = AIOperationSerializer(many=True, read_only=True)
    versions = PipelineVersionSerializer(many=True, read_only=True)
    project_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = AIPipeline
        fields = (
            "id",
            "project_id",
            "status",
            "requirements",
            "structure",
            "subsections",
            "content_plan",
            "confirmed_steps",
            "last_error",
            "operations",
            "versions",
            "created_at",
            "updated_at",
        )


class TokenLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = TokenLedgerEntry
        fields = ("id", "kind", "amount", "balance_after", "reason", "created_at")
