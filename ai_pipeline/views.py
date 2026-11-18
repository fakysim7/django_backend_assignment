import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from main.models import Projects
from platform_admin.permissions import CanGrantPlatformTokens
from platform_admin.authentication import PlatformAdminSessionAuthentication

from .models import AIOperation, AIPipeline, PipelineVersion, TokenLedgerEntry
from .serializers import AIOperationSerializer, AIPipelineSerializer, TokenLedgerSerializer
from .services import (
    PipelineError,
    account_payload,
    cancel_operation,
    create_operation,
    estimate_operation,
    get_account,
    grant_tokens,
)

User = get_user_model()


def _pipeline_response(pipeline, status_code=status.HTTP_200_OK):
    pipeline = (
        AIPipeline.objects.filter(pk=pipeline.pk)
        .prefetch_related("operations", "versions")
        .first()
    )
    return Response(AIPipelineSerializer(pipeline).data, status=status_code)


def _sync_project_blocks(project, sections):
    existing = list(project.blocks or [])
    title_page = next(
        (block for block in existing if isinstance(block, dict) and block.get("type") == "title-page"),
        None,
    )
    if title_page is None:
        title_page = {
            "id": str(uuid.uuid4()),
            "type": "title-page",
            "content": {"city": "Минск", "year": str(timezone.now().year)},
        }
    generated = {
        block.get("id"): block
        for block in existing
        if isinstance(block, dict)
        and block.get("source") == "ai_pipeline"
        and isinstance(block.get("id"), str)
    }
    manual = [
        block
        for block in existing
        if isinstance(block, dict)
        and block is not title_page
        and block.get("source") != "ai_pipeline"
    ]
    ordered = [title_page]
    current_chapter = None
    for section_index, section in enumerate(sections):
        chapter_title = section["chapter_title"]
        if chapter_title != current_chapter:
            ordered.append(
                {
                    "id": str(uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{project.id}:chapter:{section_index}:{chapter_title}",
                    )),
                    "type": "heading",
                    "source": "ai_pipeline",
                    "content": {"text": chapter_title, "level": 1},
                }
            )
            current_chapter = chapter_title
        subsection_title = section["subsection_title"]
        ordered.append(
            {
                "id": str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{project.id}:subsection:{section_index}:{subsection_title}",
                )),
                "type": "heading",
                "source": "ai_pipeline",
                "content": {"text": subsection_title, "level": 2},
            }
        )
        for planned in section["blocks"]:
            ordered.append(
                {
                    "id": str(uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{project.id}:block-title:{planned['id']}",
                    )),
                    "type": "heading",
                    "source": "ai_pipeline",
                    "content": {"text": planned["title"], "level": 3},
                }
            )
            existing_block = generated.get(planned["id"])
            if existing_block:
                ordered.append(existing_block)
                continue
            block_type = planned["type"]
            defaults = {
                "text": {"text": ""},
                "table": {"naming": planned["title"], "rows": 1, "cols": 1, "data": []},
                "chart": {
                    "title": planned["title"],
                    "chartType": "linear",
                    "series": [],
                    "isDemonstration": True,
                },
                "diagram": {"title": planned["title"], "nodes": [], "edges": []},
            }
            ordered.append(
                {
                    "id": planned["id"],
                    "type": block_type,
                    "source": "ai_pipeline",
                    "content": defaults[block_type],
                }
            )
    project.blocks = ordered + manual
    project.save(update_fields=["blocks", "updatedAt"])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_ai_project(request):
    serializer_errors = {}
    raw_name = request.data.get("name", "")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    project_type = request.data.get("type", "course")
    if not name:
        serializer_errors["name"] = ["Это поле обязательно."]
    if project_type not in {"course", "essay", "lab", "diplom"}:
        serializer_errors["type"] = ["Недопустимый тип работы."]
    requirements = request.data.get("requirements", {})
    if not isinstance(requirements, dict):
        serializer_errors["requirements"] = ["Ожидается JSON object."]
    if serializer_errors:
        return Response({"errors": serializer_errors}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        project = Projects.objects.create(
            user=request.user,
            name=name[:100],
            description=request.data.get("description", ""),
            type=project_type,
            status="active",
        )
        pipeline = AIPipeline.objects.create(
            project=project,
            user=request.user,
            status=AIPipeline.Status.COLLECTING_REQUIREMENTS,
            requirements=requirements,
        )
    return Response(
        {
            "project": {
                "id": str(project.id),
                "name": project.name,
                "type": project.type,
                "status": project.status,
            },
            "pipeline": AIPipelineSerializer(pipeline).data,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def pipeline_detail(request, project_id):
    project = get_object_or_404(Projects, pk=project_id, user=request.user)
    pipeline = get_object_or_404(AIPipeline, project=project, user=request.user)
    if request.method == "PATCH":
        requirements = request.data.get("requirements")
        if not isinstance(requirements, dict):
            return Response(
                {"error": "requirements must be a JSON object"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        pipeline.requirements = requirements
        pipeline.status = AIPipeline.Status.COLLECTING_REQUIREMENTS
        pipeline.last_error = ""
        pipeline.save(update_fields=["requirements", "status", "last_error", "updated_at"])
        topic = requirements.get("topic")
        if isinstance(topic, str) and topic.strip():
            project.name = topic.strip()[:100]
            project.save(update_fields=["name", "updatedAt"])
    return _pipeline_response(pipeline)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def start_operation(request, project_id):
    project = get_object_or_404(Projects, pk=project_id, user=request.user)
    pipeline = get_object_or_404(AIPipeline, project=project, user=request.user)
    task_type = request.data.get("task_type")
    request_data = request.data.get("payload", {})
    if task_type == "subsections" and "structure" not in pipeline.confirmed_steps:
        return Response({"error": "Confirm the structure before generating subsections"}, status=409)
    if task_type in {"content_plan", "text", "table", "chart", "diagram"} and (
        "subsections" not in pipeline.confirmed_steps
        if task_type == "content_plan"
        else "content_plan" not in pipeline.confirmed_steps
    ):
        return Response({"error": "Confirm the previous pipeline stage first"}, status=409)
    if task_type in {"text", "table", "chart", "diagram"}:
        block_id = request_data.get("block_id") if isinstance(request_data, dict) else None
        if not isinstance(block_id, str) or not block_id:
            return Response({"error": "payload.block_id is required"}, status=400)
        planned = next(
            (
                block
                for section in pipeline.content_plan
                if isinstance(section, dict)
                for block in section.get("blocks", [])
                if isinstance(block, dict) and block.get("id") == block_id
            ),
            None,
        )
        if not planned or planned.get("type") != task_type:
            return Response({"error": "The block does not match the confirmed content plan"}, status=400)
        request_data = {**planned, "block_id": block_id}
    elif task_type == "content_plan" and "subsections" in request_data:
        request_data = {"subsections": pipeline.subsections}
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        return Response({"error": "Idempotency-Key header is required"}, status=400)
    try:
        operation, created = create_operation(
            pipeline, request.user, task_type, request_data, key[:120]
        )
    except PipelineError as exc:
        code = status.HTTP_402_PAYMENT_REQUIRED if "balance" in str(exc).lower() else status.HTTP_403_FORBIDDEN
        return Response({"error": str(exc)}, status=code)
    return Response(
        AIOperationSerializer(operation).data,
        status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@transaction.atomic
def confirm_stage(request, project_id):
    project = get_object_or_404(Projects, pk=project_id, user=request.user)
    pipeline = get_object_or_404(AIPipeline, project=project, user=request.user)
    stage = request.data.get("stage")
    field_by_stage = {
        "structure": "structure",
        "subsections": "subsections",
        "content_plan": "content_plan",
    }
    if stage == "document":
        if "content_plan" not in pipeline.confirmed_steps:
            return Response({"error": "Confirm the content plan first"}, status=409)
        blocks = {
            block.get("id"): block
            for block in project.blocks
            if isinstance(block, dict) and isinstance(block.get("id"), str)
        }
        missing = []
        for section in pipeline.content_plan:
            for planned in section.get("blocks", []):
                current = blocks.get(planned.get("id"))
                content = current.get("content", {}) if current else {}
                valid_content = bool(content)
                if planned.get("type") == "text":
                    valid_content = isinstance(content.get("text"), str) and bool(
                        content["text"].strip()
                    )
                elif planned.get("type") == "table":
                    valid_content = (
                        isinstance(content.get("data"), list)
                        and bool(content["data"])
                        and content.get("rows", 0) > 0
                        and content.get("cols", 0) > 0
                    )
                elif planned.get("type") == "chart":
                    valid_content = isinstance(content.get("series"), list) and bool(
                        content["series"]
                    )
                elif planned.get("type") == "diagram":
                    valid_content = isinstance(content.get("nodes"), list) and bool(
                        content["nodes"]
                    )
                if not valid_content:
                    missing.append(planned.get("title", planned.get("id", "block")))
        if missing:
            return Response(
                {"error": "Some planned blocks are not generated", "missing": missing},
                status=409,
            )
        pipeline.status = AIPipeline.Status.COMPLETED
        pipeline.confirmed_steps = list(
            dict.fromkeys([*pipeline.confirmed_steps, stage])
        )
        pipeline.last_error = ""
        pipeline.save(update_fields=["status", "confirmed_steps", "last_error", "updated_at"])
        return _pipeline_response(pipeline)
    field = field_by_stage.get(stage)
    if not field:
        return Response({"error": "Unknown stage"}, status=status.HTTP_400_BAD_REQUEST)
    data = request.data.get("data", getattr(pipeline, field))
    if not isinstance(data, list) or not data:
        return Response({"error": f"{stage} must be a non-empty array"}, status=400)
    if stage == "structure" and not all(
        isinstance(item, dict)
        and isinstance(item.get("title"), str)
        and item.get("title").strip()
        for item in data
    ):
        return Response({"error": "Each chapter must have a title"}, status=400)
    if stage == "subsections" and not all(
        isinstance(item, dict)
        and isinstance(item.get("chapter_title"), str)
        and item.get("chapter_title").strip()
        and isinstance(item.get("items"), list)
        and bool(item.get("items"))
        and all(
            isinstance(subsection, dict)
            and isinstance(subsection.get("title"), str)
            and subsection["title"].strip()
            for subsection in item["items"]
        )
        for item in data
    ):
        return Response({"error": "Each subsection group must have an items array"}, status=400)
    if stage == "subsections" and "structure" not in pipeline.confirmed_steps:
        return Response({"error": "Confirm the structure first"}, status=409)
    if stage == "content_plan" and not all(
        isinstance(item, dict)
        and isinstance(item.get("chapter_title"), str)
        and item.get("chapter_title").strip()
        and isinstance(item.get("subsection_title"), str)
        and item.get("subsection_title").strip()
        and isinstance(item.get("blocks"), list)
        and bool(item.get("blocks"))
        for item in data
    ):
        return Response({"error": "Each plan section must have a blocks array"}, status=400)
    if stage == "content_plan" and "subsections" not in pipeline.confirmed_steps:
        return Response({"error": "Confirm the subsections first"}, status=409)
    if stage == "content_plan":
        ids = []
        for section in data:
            for block in section["blocks"]:
                if (
                    not isinstance(block, dict)
                    or not isinstance(block.get("id"), str)
                    or not block.get("id")
                    or len(block["id"]) > 80
                    or block.get("type") not in {"text", "table", "chart", "diagram"}
                    or not isinstance(block.get("title"), str)
                    or not block.get("title").strip()
                ):
                    return Response({"error": "Content plan contains an invalid block"}, status=400)
                ids.append(block["id"])
        if len(ids) != len(set(ids)):
            return Response({"error": "Content plan block ids must be unique"}, status=400)

    if stage == "content_plan":
        snapshot_version = (
            pipeline.versions.filter(stage="project_blocks")
            .order_by("-version")
            .values_list("version", flat=True)
            .first()
            or 0
        ) + 1
        PipelineVersion.objects.create(
            pipeline=pipeline,
            stage="project_blocks",
            version=snapshot_version,
            data=list(project.blocks or []),
        )
        _sync_project_blocks(project, data)

    setattr(pipeline, field, data)
    downstream = {
        "structure": {"subsections", "content_plan", "document"},
        "subsections": {"content_plan", "document"},
        "content_plan": {"document"},
    }
    pipeline.confirmed_steps = [
        item
        for item in dict.fromkeys([*pipeline.confirmed_steps, stage])
        if item not in downstream[stage]
    ]
    pipeline.last_error = ""
    pipeline.save(update_fields=[field, "confirmed_steps", "last_error", "updated_at"])
    latest = pipeline.operations.filter(
        status=AIOperation.Status.SUCCEEDED, task_type=stage
    ).first()
    version = (
        pipeline.versions.filter(stage=stage).order_by("-version").values_list("version", flat=True).first()
        or 0
    ) + 1
    PipelineVersion.objects.create(
        pipeline=pipeline,
        operation=latest,
        stage=stage,
        version=version,
        data=data,
    )
    return _pipeline_response(pipeline)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def operation_detail(request, operation_id):
    operation = get_object_or_404(AIOperation, pk=operation_id, user=request.user)
    return Response(AIOperationSerializer(operation).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_ai_operation(request, operation_id):
    operation = get_object_or_404(AIOperation, pk=operation_id, user=request.user)
    operation = cancel_operation(operation)
    return Response(AIOperationSerializer(operation).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def restore_version(request, project_id, version_id):
    project = get_object_or_404(Projects, pk=project_id, user=request.user)
    pipeline = get_object_or_404(AIPipeline, project=project, user=request.user)
    version = get_object_or_404(PipelineVersion, pk=version_id, pipeline=pipeline)
    fields = {
        "structure": "structure",
        "subsections": "subsections",
        "content_plan": "content_plan",
    }
    if version.stage in fields:
        field = fields[version.stage]
        setattr(pipeline, field, version.data)
        downstream = {
            "structure": {"subsections", "content_plan", "document"},
            "subsections": {"content_plan", "document"},
            "content_plan": {"document"},
        }
        pipeline.confirmed_steps = [
            item
            for item in dict.fromkeys([*pipeline.confirmed_steps, version.stage])
            if item not in downstream[version.stage]
        ]
        pipeline.status = {
            "structure": AIPipeline.Status.STRUCTURE_REVIEW,
            "subsections": AIPipeline.Status.SUBSECTIONS_REVIEW,
            "content_plan": AIPipeline.Status.CONTENT_PLAN_REVIEW,
        }[version.stage]
        pipeline.last_error = ""
        pipeline.save(
            update_fields=[
                field,
                "confirmed_steps",
                "status",
                "last_error",
                "updated_at",
            ]
        )
    elif version.stage == "project_blocks" and isinstance(version.data, list):
        project.blocks = version.data
        project.save(update_fields=["blocks", "updatedAt"])
    elif version.stage.startswith("block:") and isinstance(version.data, dict):
        block_id = version.stage.removeprefix("block:")
        if version.data.get("id") != block_id:
            return Response({"error": "Stored block version is invalid"}, status=400)
        blocks = list(project.blocks or [])
        for index, block in enumerate(blocks):
            if isinstance(block, dict) and block.get("id") == block_id:
                blocks[index] = version.data
                break
        else:
            blocks.append(version.data)
        project.blocks = blocks
        project.save(update_fields=["blocks", "updatedAt"])
    else:
        return Response({"error": "Unsupported version type"}, status=400)
    next_version = (
        pipeline.versions.filter(stage=version.stage)
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
        or 0
    ) + 1
    PipelineVersion.objects.create(
        pipeline=pipeline,
        stage=version.stage,
        version=next_version,
        data=version.data,
    )
    return _pipeline_response(pipeline)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def account_detail(request):
    entitlement, wallet = get_account(request.user)
    ledger = TokenLedgerEntry.objects.filter(wallet=wallet)[:50]
    return Response(
        {
            **account_payload(request.user),
            "ledger": TokenLedgerSerializer(ledger, many=True).data,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def estimate(request):
    task_type = request.query_params.get("task_type", "")
    try:
        tokens = estimate_operation(task_type)
    except PipelineError as exc:
        return Response({"error": str(exc)}, status=400)
    return Response({"task_type": task_type, "estimated_tokens": tokens})


@api_view(["POST"])
@authentication_classes([PlatformAdminSessionAuthentication])
@permission_classes([IsAuthenticated, CanGrantPlatformTokens])
def admin_grant(request):
    user_id = request.data.get("user_id")
    amount = request.data.get("amount")
    reason = request.data.get("reason", "").strip()
    key = request.headers.get("Idempotency-Key", "").strip()
    if not reason or not key or isinstance(amount, bool) or not isinstance(amount, int):
        return Response(
            {"error": "user_id, positive integer amount, reason and Idempotency-Key are required"},
            status=400,
        )
    user = get_object_or_404(User, user_id=user_id)
    try:
        balance = grant_tokens(user, amount, reason, key[:120])
    except PipelineError as exc:
        return Response({"error": str(exc)}, status=400)
    return Response({"user_id": str(user.user_id), "balance": balance})
