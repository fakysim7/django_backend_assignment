import uuid

from django.conf import settings
from django.db import models


class AIPlan(models.Model):
    code = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=80)
    features = models.JSONField(default=list)
    daily_operation_limit = models.PositiveIntegerField(default=20)
    max_operation_cost = models.PositiveIntegerField(default=100)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class UserEntitlement(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_entitlement",
    )
    plan = models.ForeignKey(AIPlan, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)


class TokenWallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_token_wallet",
    )
    balance = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class TokenLedgerEntry(models.Model):
    class Kind(models.TextChoices):
        GRANT = "grant", "Grant"
        RESERVED = "reserved", "Reserved"
        CHARGE = "charge", "Charge"
        REFUND = "refund", "Refund"

    wallet = models.ForeignKey(
        TokenWallet, on_delete=models.CASCADE, related_name="entries"
    )
    operation = models.ForeignKey(
        "AIOperation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ledger_entries",
    )
    kind = models.CharField(max_length=12, choices=Kind.choices)
    amount = models.IntegerField()
    balance_after = models.PositiveIntegerField()
    reason = models.CharField(max_length=240)
    idempotency_key = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class AIPipeline(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        COLLECTING_REQUIREMENTS = "collecting_requirements", "Collecting requirements"
        GENERATING_STRUCTURE = "generating_structure", "Generating structure"
        STRUCTURE_REVIEW = "structure_review", "Structure review"
        GENERATING_SUBSECTIONS = "generating_subsections", "Generating subsections"
        SUBSECTIONS_REVIEW = "subsections_review", "Subsections review"
        GENERATING_CONTENT_PLAN = "generating_content_plan", "Generating content plan"
        CONTENT_PLAN_REVIEW = "content_plan_review", "Content plan review"
        GENERATING_DOCUMENT = "generating_document", "Generating document"
        DOCUMENT_REVIEW = "document_review", "Document review"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField(
        "main.Projects", on_delete=models.CASCADE, related_name="ai_pipeline"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_pipelines"
    )
    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.DRAFT
    )
    requirements = models.JSONField(default=dict)
    structure = models.JSONField(default=list)
    subsections = models.JSONField(default=list)
    content_plan = models.JSONField(default=list)
    confirmed_steps = models.JSONField(default=list)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class AIOperation(models.Model):
    class TaskType(models.TextChoices):
        STRUCTURE = "structure", "Structure"
        SUBSECTIONS = "subsections", "Subsections"
        CONTENT_PLAN = "content_plan", "Content plan"
        TEXT = "text", "Text block"
        TABLE = "table", "Table block"
        CHART = "chart", "Chart block"
        DIAGRAM = "diagram", "Process diagram"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pipeline = models.ForeignKey(
        AIPipeline, on_delete=models.CASCADE, related_name="operations"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    task_type = models.CharField(max_length=20, choices=TaskType.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    request_data = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    error = models.TextField(blank=True)
    model_name = models.CharField(max_length=160, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    provider_cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True, default=None
    )
    reserved_tokens = models.PositiveIntegerField(default=0)
    charged_tokens = models.PositiveIntegerField(default=0)
    worker_attempts = models.PositiveIntegerField(default=0)
    lease_owner = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                name="ai_operation_user_idempotency_unique",
            )
        ]
        indexes = [
            models.Index(fields=["status", "created_at"], name="ai_op_status_created_idx"),
            models.Index(fields=["status", "lease_expires_at"], name="ai_op_lease_expiry_idx"),
        ]


class AIProviderAttempt(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    operation = models.ForeignKey(
        AIOperation, on_delete=models.CASCADE, related_name="provider_attempts"
    )
    attempt_number = models.PositiveIntegerField()
    model_name = models.CharField(max_length=160)
    status = models.CharField(max_length=12, choices=Status.choices)
    is_retryable = models.BooleanField(default=False)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    provider_cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True
    )
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["attempt_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["operation", "attempt_number"],
                name="ai_provider_attempt_number_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=["operation", "status"], name="ai_provider_op_status_idx"
            )
        ]


class PipelineVersion(models.Model):
    pipeline = models.ForeignKey(
        AIPipeline, on_delete=models.CASCADE, related_name="versions"
    )
    operation = models.ForeignKey(
        AIOperation, null=True, blank=True, on_delete=models.SET_NULL
    )
    stage = models.CharField(max_length=80)
    version = models.PositiveIntegerField()
    data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["stage", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["pipeline", "stage", "version"],
                name="ai_pipeline_stage_version_unique",
            )
        ]
