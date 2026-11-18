import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from main.models import Projects

from .models import (
    AIPlan,
    AIOperation,
    AIProviderAttempt,
    AIPipeline,
    PipelineVersion,
    TokenLedgerEntry,
    TokenWallet,
    UserEntitlement,
)

logger = logging.getLogger(__name__)

DEFAULT_FEATURES = [
    "structure", "subsections", "content_plan", "text", "table", "chart", "diagram"
]
DEFAULT_COSTS = {
    "structure": 5,
    "subsections": 5,
    "content_plan": 8,
    "text": 10,
    "table": 10,
    "chart": 10,
    "diagram": 10,
}
GENERATING_STATUS = {
    "structure": AIPipeline.Status.GENERATING_STRUCTURE,
    "subsections": AIPipeline.Status.GENERATING_SUBSECTIONS,
    "content_plan": AIPipeline.Status.GENERATING_CONTENT_PLAN,
    "text": AIPipeline.Status.GENERATING_DOCUMENT,
    "table": AIPipeline.Status.GENERATING_DOCUMENT,
    "chart": AIPipeline.Status.GENERATING_DOCUMENT,
    "diagram": AIPipeline.Status.GENERATING_DOCUMENT,
}


class PipelineError(Exception):
    pass


class RetryablePipelineError(PipelineError):
    pass


def _json_setting(name, default):
    raw = getattr(settings, name, "")
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"{name} must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"{name} must be a JSON object")
    return value


def operation_cost(task_type):
    configured = _json_setting("AI_OPERATION_COSTS_JSON", {})
    value = configured.get(task_type, DEFAULT_COSTS[task_type])
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PipelineError(f"Invalid token price for {task_type}")
    return value


def get_account(user):
    plan, _ = AIPlan.objects.get_or_create(
        code="free",
        defaults={
            "name": "Free",
            "features": DEFAULT_FEATURES,
            "daily_operation_limit": 20,
            "max_operation_cost": 25,
        },
    )
    entitlement, _ = UserEntitlement.objects.get_or_create(user=user, defaults={"plan": plan})
    wallet, _ = TokenWallet.objects.get_or_create(user=user)
    return entitlement, wallet


def account_payload(user):
    entitlement, wallet = get_account(user)
    return {
        "balance": wallet.balance,
        "plan": {
            "code": entitlement.plan.code,
            "name": entitlement.plan.name,
            "features": entitlement.plan.features,
            "daily_operation_limit": entitlement.plan.daily_operation_limit,
            "max_operation_cost": entitlement.plan.max_operation_cost,
        },
    }


def _reserve_tokens(operation):
    with transaction.atomic():
        entitlement, wallet = get_account(operation.user)
        cost = operation_cost(operation.task_type)
        if cost > entitlement.plan.max_operation_cost:
            raise PipelineError("This operation exceeds the active plan's per-operation limit")
        since = timezone.now() - timedelta(days=1)
        recent_count = AIOperation.objects.filter(
            user=operation.user, created_at__gte=since
        ).exclude(status=AIOperation.Status.CANCELLED).count()
        if recent_count >= entitlement.plan.daily_operation_limit:
            raise PipelineError("Daily AI operation limit reached")
        if operation.task_type not in entitlement.plan.features:
            raise PipelineError("The active plan does not include this AI feature")
        if not entitlement.plan.is_active:
            raise PipelineError("The active plan is disabled")

        wallet = TokenWallet.objects.select_for_update().get(pk=wallet.pk)
        reservation_key = f"reserve:{operation.id}"
        existing = TokenLedgerEntry.objects.filter(
            idempotency_key=reservation_key
        ).first()
        if existing:
            if existing.operation_id != operation.id or existing.amount >= 0:
                raise PipelineError("Reservation key conflicts with an earlier ledger operation")
            operation.reserved_tokens = -existing.amount
            operation.save(update_fields=["reserved_tokens"])
            return
        if wallet.balance < cost:
            raise PipelineError("Insufficient token balance")
        wallet.balance -= cost
        wallet.save(update_fields=["balance", "updated_at"])
        operation.reserved_tokens = cost
        operation.save(update_fields=["reserved_tokens"])
        TokenLedgerEntry.objects.create(
            wallet=wallet,
            operation=operation,
            kind=TokenLedgerEntry.Kind.RESERVED,
            amount=-cost,
            balance_after=wallet.balance,
            reason=f"Reserved for {operation.task_type} generation",
            idempotency_key=reservation_key,
        )


def create_operation(pipeline, user, task_type, request_data, idempotency_key):
    allowed = {choice[0] for choice in AIOperation.TaskType.choices}
    if task_type not in allowed:
        raise PipelineError("Unsupported AI task type")
    if not isinstance(request_data, dict):
        raise PipelineError("Operation payload must be a JSON object")

    existing = AIOperation.objects.filter(
        user=user, idempotency_key=idempotency_key
    ).first()
    if existing:
        if existing.pipeline_id != pipeline.id:
            raise PipelineError("Idempotency key has already been used for another pipeline")
        return existing, False

    try:
        with transaction.atomic():
            locked_pipeline = AIPipeline.objects.select_for_update().get(pk=pipeline.pk)
            if locked_pipeline.operations.filter(
                status__in=[AIOperation.Status.QUEUED, AIOperation.Status.RUNNING]
            ).exists():
                raise PipelineError("Another operation is already running for this project")
            operation = AIOperation.objects.create(
                pipeline=locked_pipeline,
                user=user,
                task_type=task_type,
                request_data=request_data,
                idempotency_key=idempotency_key,
            )
            _reserve_tokens(operation)
            locked_pipeline.status = GENERATING_STATUS[task_type]
            locked_pipeline.last_error = ""
            locked_pipeline.save(update_fields=["status", "last_error", "updated_at"])
            return operation, True
    except IntegrityError:
        operation = AIOperation.objects.filter(
            user=user, idempotency_key=idempotency_key
        ).first()
        if operation:
            return operation, False
        raise


def grant_tokens(user, amount, reason, idempotency_key):
    if amount <= 0:
        raise PipelineError("Grant amount must be positive")
    with transaction.atomic():
        _, wallet = get_account(user)
        wallet = TokenWallet.objects.select_for_update().get(pk=wallet.pk)
        entry, created = TokenLedgerEntry.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "wallet": wallet,
                "kind": TokenLedgerEntry.Kind.GRANT,
                "amount": amount,
                "balance_after": wallet.balance + amount,
                "reason": reason,
            },
        )
        if not created:
            if (
                entry.wallet_id != wallet.id
                or entry.amount != amount
                or entry.kind != TokenLedgerEntry.Kind.GRANT
            ):
                raise PipelineError("Idempotency key conflicts with an earlier ledger operation")
            return wallet.balance
        wallet.balance += amount
        wallet.save(update_fields=["balance", "updated_at"])
        entry.balance_after = wallet.balance
        entry.save(update_fields=["balance_after"])
        return wallet.balance


def _task_prompt(operation):
    pipeline = operation.pipeline
    req = pipeline.requirements
    data = operation.request_data
    task = operation.task_type
    if task == AIOperation.TaskType.STRUCTURE:
        instructions = (
            "Create an academic work outline. Return JSON {\"chapters\":[{\"title\":string,"
            "\"order\":integer}]}. Include introduction and conclusion. Avoid invented facts."
        )
        context = {"requirements": req, "existing": pipeline.structure}
    elif task == AIOperation.TaskType.SUBSECTIONS:
        instructions = (
            "Create subsections for each confirmed chapter. Return JSON "
            "{\"subsections\":[{\"chapter_id\":string,\"chapter_title\":string,"
            "\"items\":[{\"title\":string,\"order\":integer}]}]}. Preserve chapter ids and order."
        )
        context = {"requirements": req, "chapters": data.get("chapters", pipeline.structure)}
    elif task == AIOperation.TaskType.CONTENT_PLAN:
        instructions = (
            "Create a granular content plan. Return JSON {\"sections\":[{\"chapter_id\":string,"
            "\"chapter_title\":string,\"subsection_title\":string,\"blocks\":[{\"id\":string,"
            "\"type\":\"text\"|\"table\"|\"chart\"|\"diagram\",\"title\":string,\"description\":string,"
            "\"paragraphs\":integer}]}]}. Do not produce a long document."
        )
        context = {
            "requirements": req,
            "structure": pipeline.structure,
            "subsections": data.get("subsections", pipeline.subsections),
        }
    elif task == AIOperation.TaskType.TEXT:
        instructions = (
            "Write only the requested academic content block. Return JSON "
            "{\"paragraphs\":[string,...]}. Do not invent numerical facts or citations."
        )
        context = {"requirements": req, "block": data}
    elif task == AIOperation.TaskType.TABLE:
        instructions = (
            "Create a useful table for the requested content. Return JSON "
            "{\"title\":string,\"columns\":[string,...],\"rows\":[[string,...],...],"
            "\"source_note\":string}. Label illustrative data as demonstration."
        )
        context = {"requirements": req, "block": data}
    elif task == AIOperation.TaskType.CHART:
        instructions = (
            "Create structured chart data only. Return JSON {\"title\":string,\"description\":string,"
            "\"chart_type\":\"linear\"|\"area\"|\"bar\"|\"scatter\"|\"pie\","
            "\"x_axis\":string,\"y_axis\":string,\"series\":[{\"name\":string,"
            "\"points\":[{\"x\":string,\"y\":number}]}],\"is_demonstration\":boolean}."
            "Never present invented numbers as factual."
        )
        context = {"requirements": req, "block": data}
    else:
        instructions = (
            "Create a structured process diagram, not a statistical chart. Return JSON "
            "{\"title\":string,\"diagram_type\":\"flowchart\"|\"sequence\","
            "\"nodes\":[{\"id\":string,\"label\":string}],"
            "\"edges\":[{\"from\":string,\"to\":string,\"label\":string}]}. "
            "Keep the diagram concise and ensure every edge references existing nodes."
        )
        context = {"requirements": req, "confirmed_plan": pipeline.content_plan, "block": data}
    return instructions, context


def _router_config(task_type):
    routing = _json_setting("OPENROUTER_MODEL_ROUTING_JSON", {})
    config = routing.get(task_type) or {}
    if not isinstance(config, dict):
        raise PipelineError(f"Invalid model routing configuration for {task_type}")
    model = config.get("model") or getattr(settings, "OPENROUTER_DEFAULT_MODEL", "")
    fallbacks = config.get("fallbacks", [])
    if not isinstance(fallbacks, list) or not all(isinstance(item, str) for item in fallbacks):
        raise PipelineError("Model fallbacks must be a list of model names")
    models = [item for item in [model, *fallbacks] if item]
    if not models:
        raise PipelineError("No OpenRouter model is configured for this operation")
    return models


def _provider_attempt_limit():
    value = getattr(settings, "AI_PROVIDER_RETRY_ATTEMPTS", 2)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PipelineError("AI_PROVIDER_RETRY_ATTEMPTS must be a positive integer")
    return min(value, 5)


def _usage_value(usage, name):
    value = usage.get(name)
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _provider_cost(body):
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    value = usage.get("cost")
    if value is None:
        value = body.get("cost")
    if value is None:
        return None
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return cost if cost.is_finite() and cost >= 0 else None


def _is_temporary_provider_error(error):
    if isinstance(error, requests.HTTPError):
        response = error.response
        code = response.status_code if response is not None else None
        return code in {408, 425, 429} or (code is not None and code >= 500)
    return isinstance(error, requests.RequestException)


def _call_openrouter(operation):
    api_key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not api_key:
        raise PipelineError("OPENROUTER_API_KEY is not configured on the backend")
    instructions, context = _task_prompt(operation)
    response_format = {"type": "json_object"}
    last_error = None
    last_error_retryable = False
    retry_limit = _provider_attempt_limit()
    for model in _router_config(operation.task_type):
        for retry_index in range(retry_limit):
            attempt_number = (
                AIProviderAttempt.objects.filter(operation=operation).count() + 1
            )
            attempt = AIProviderAttempt.objects.create(
                operation=operation,
                attempt_number=attempt_number,
                model_name=model,
                status=AIProviderAttempt.Status.RUNNING,
            )
            response = None
            body = None
            error = None
            result = None
            try:
                response = requests.post(
                    getattr(settings, "OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions"),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": getattr(settings, "OPENROUTER_SITE_URL", ""),
                        "X-Title": getattr(settings, "OPENROUTER_APP_NAME", "Fine PAPERs"),
                    },
                    json={
                        "model": model,
                        "max_tokens": getattr(settings, "OPENROUTER_MAX_OUTPUT_TOKENS", 4000),
                        "messages": [
                            {"role": "system", "content": instructions},
                            {
                                "role": "user",
                                "content": json.dumps(context, ensure_ascii=False)[
                                    : getattr(settings, "AI_MAX_CONTEXT_CHARS", 16000)
                                ],
                            },
                        ],
                        "response_format": response_format,
                    },
                    timeout=getattr(settings, "OPENROUTER_TIMEOUT_SECONDS", 90),
                )
                try:
                    body = response.json()
                except ValueError:
                    body = None
                usage = body.get("usage") if isinstance(body, dict) else None
                usage = usage if isinstance(usage, dict) else {}
                attempt.input_tokens = _usage_value(usage, "prompt_tokens")
                attempt.output_tokens = _usage_value(usage, "completion_tokens")
                attempt.provider_cost_usd = _provider_cost(body)
                response.raise_for_status()
                content = body["choices"][0]["message"]["content"]
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise PipelineError("Model response must be a JSON object")
                _validate_result(operation.task_type, result)
            except (
                requests.RequestException,
                ValueError,
                KeyError,
                TypeError,
                IndexError,
                PipelineError,
            ) as exc:
                error = exc
                retryable = _is_temporary_provider_error(exc)
                attempt.status = AIProviderAttempt.Status.FAILED
                attempt.is_retryable = retryable
                attempt.error = str(exc)[:2000]
                attempt.completed_at = timezone.now()
                attempt.save(
                    update_fields=[
                        "input_tokens",
                        "output_tokens",
                        "provider_cost_usd",
                        "status",
                        "is_retryable",
                        "error",
                        "completed_at",
                    ]
                )
                last_error = exc
                last_error_retryable = retryable
                logger.warning("OpenRouter model %s failed: %s", model, exc)
                if retryable and retry_index + 1 < retry_limit:
                    continue
                break

            attempt.status = AIProviderAttempt.Status.SUCCEEDED
            attempt.completed_at = timezone.now()
            attempt.save(
                update_fields=[
                    "input_tokens",
                    "output_tokens",
                    "provider_cost_usd",
                    "status",
                    "completed_at",
                ]
            )
            return {
                "result": result,
                "model": model,
                "input_tokens": attempt.input_tokens or 0,
                "output_tokens": attempt.output_tokens or 0,
                "provider_cost_usd": _operation_provider_cost(operation),
            }

    error_message = f"All configured OpenRouter models failed: {last_error}"
    if last_error_retryable:
        raise RetryablePipelineError(error_message) from last_error
    raise PipelineError(error_message) from last_error


def _operation_provider_cost(operation, fallback=None):
    costs = list(
        AIProviderAttempt.objects.filter(operation=operation).values_list(
            "provider_cost_usd", flat=True
        )
    )
    if not costs:
        return fallback
    if any(cost is None for cost in costs):
        return None
    return sum(costs, Decimal("0"))


def _validate_result(task_type, result):
    expected = {
        "structure": ("chapters", list),
        "subsections": ("subsections", list),
        "content_plan": ("sections", list),
        "text": ("paragraphs", list),
        "table": ("columns", list),
        "chart": ("series", list),
        "diagram": ("nodes", list),
    }
    key, expected_type = expected[task_type]
    value = result.get(key)
    if not isinstance(value, expected_type) or not value:
        raise PipelineError(f"AI response is missing a non-empty '{key}' array")
    if task_type == "structure":
        if not all(isinstance(item, dict) and isinstance(item.get("title"), str) for item in value):
            raise PipelineError("AI outline contains an invalid chapter")
    elif task_type == "subsections":
        if not all(isinstance(item, dict) and isinstance(item.get("items"), list) for item in value):
            raise PipelineError("AI response contains an invalid subsection group")
    elif task_type == "content_plan":
        if not all(isinstance(item, dict) and isinstance(item.get("blocks"), list) for item in value):
            raise PipelineError("AI response contains an invalid content plan section")
    elif task_type == "text":
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise PipelineError("AI response contains an invalid text paragraph")
    elif task_type == "table":
        if not all(isinstance(item, str) for item in value):
            raise PipelineError("AI response contains invalid table columns")
        rows = result.get("rows")
        if not isinstance(rows, list) or not rows or not all(
            isinstance(row, list) and len(row) == len(value) for row in rows
        ):
            raise PipelineError("AI response contains invalid table rows")
    elif task_type == "chart":
        if not all(isinstance(series, dict) and isinstance(series.get("points"), list) for series in value):
            raise PipelineError("AI response contains an invalid chart series")
        if result.get("chart_type") not in {"linear", "area", "bar", "scatter", "pie"}:
            raise PipelineError("AI response contains an unsupported chart type")
        if not isinstance(result.get("is_demonstration"), bool):
            raise PipelineError("AI response must identify demonstration data")
        for series in value:
            if not all(
                isinstance(point, dict)
                and isinstance(point.get("x"), str)
                and isinstance(point.get("y"), (int, float))
                and not isinstance(point.get("y"), bool)
                for point in series["points"]
            ):
                raise PipelineError("AI response contains invalid chart points")
    else:
        nodes = result["nodes"]
        node_ids = {
            node.get("id")
            for node in nodes
            if isinstance(node, dict)
            and isinstance(node.get("id"), str)
            and isinstance(node.get("label"), str)
        }
        if len(node_ids) != len(nodes) or not node_ids:
            raise PipelineError("AI response contains invalid diagram nodes")
        edges = result.get("edges")
        if not isinstance(edges, list) or not all(
            isinstance(edge, dict)
            and edge.get("from") in node_ids
            and edge.get("to") in node_ids
            for edge in edges
        ):
            raise PipelineError("AI response contains invalid diagram edges")
        if result.get("diagram_type") not in {"flowchart", "sequence"}:
            raise PipelineError("AI response contains an unsupported diagram type")


def _save_version(pipeline, operation, stage, data):
    version = (pipeline.versions.filter(stage=stage).order_by("-version").values_list("version", flat=True).first() or 0) + 1
    PipelineVersion.objects.create(
        pipeline=pipeline,
        operation=operation,
        stage=stage,
        version=version,
        data=data,
    )


def _apply_result(operation, result):
    pipeline = AIPipeline.objects.select_for_update().get(pk=operation.pipeline_id)
    project = Projects.objects.select_for_update().get(pk=pipeline.project_id, user=operation.user)
    task = operation.task_type
    if task == "structure":
        pipeline.status = AIPipeline.Status.STRUCTURE_REVIEW
        _save_version(pipeline, operation, "structure", result["chapters"])
    elif task == "subsections":
        pipeline.status = AIPipeline.Status.SUBSECTIONS_REVIEW
        _save_version(pipeline, operation, "subsections", result["subsections"])
    elif task == "content_plan":
        pipeline.status = AIPipeline.Status.CONTENT_PLAN_REVIEW
        _save_version(pipeline, operation, "content_plan", result["sections"])
    elif task in ("text", "table", "chart", "diagram"):
        block_id = operation.request_data.get("block_id")
        if not isinstance(block_id, str) or not block_id:
            raise PipelineError("block_id is required for content generation")
        block_type = {
            "text": "text",
            "table": "table",
            "chart": "chart",
            "diagram": "diagram",
        }[task]
        content = result
        if task == "text":
            content = {"text": "\n\n".join(result["paragraphs"])}
        elif task == "table":
            rows = result["rows"]
            data = list(result["columns"])
            for row in rows:
                data.extend(row)
            content = {
                "naming": result.get("title", ""),
                "rows": len(rows) + 1,
                "cols": len(result["columns"]),
                "data": data,
                "source": result.get("source_note", ""),
            }
        elif task == "chart":
            content = {
                "title": result["title"],
                "description": result.get("description", ""),
                "chartType": result["chart_type"],
                "xAxis": result.get("x_axis", ""),
                "yAxis": result.get("y_axis", ""),
                "series": result["series"],
                "isDemonstration": result["is_demonstration"],
            }
        blocks = list(project.blocks or [])
        replacement = {
            "id": block_id,
            "type": block_type,
            "source": "ai_pipeline",
            "content": content,
        }
        for index, block in enumerate(blocks):
            if isinstance(block, dict) and block.get("id") == block_id:
                blocks[index] = replacement
                break
        else:
            blocks.append(replacement)
        project.blocks = blocks
        project.save(update_fields=["blocks", "updatedAt"])
        pipeline.status = AIPipeline.Status.DOCUMENT_REVIEW
        _save_version(pipeline, operation, f"block:{block_id}", replacement)
    pipeline.last_error = ""
    pipeline.save(update_fields=["status", "last_error", "structure", "subsections", "content_plan", "updated_at"])


def _refund(operation, reason):
    amount = operation.reserved_tokens
    if amount <= 0:
        return False

    with transaction.atomic():
        wallet = TokenWallet.objects.select_for_update().get(user=operation.user)
        key = f"refund:{operation.id}"
        existing = TokenLedgerEntry.objects.filter(idempotency_key=key).first()
        if existing:
            if (
                existing.wallet_id != wallet.id
                or existing.operation_id != operation.id
                or existing.kind != TokenLedgerEntry.Kind.REFUND
                or existing.amount != amount
            ):
                raise PipelineError("Refund key conflicts with an earlier ledger operation")
            return False
        try:
            with transaction.atomic():
                entry = TokenLedgerEntry.objects.create(
                    wallet=wallet,
                    operation=operation,
                    kind=TokenLedgerEntry.Kind.REFUND,
                    amount=amount,
                    balance_after=wallet.balance + amount,
                    reason=reason[:240],
                    idempotency_key=key,
                )
        except IntegrityError:
            existing = TokenLedgerEntry.objects.filter(idempotency_key=key).first()
            if (
                existing
                and existing.wallet_id == wallet.id
                and existing.operation_id == operation.id
                and existing.kind == TokenLedgerEntry.Kind.REFUND
                and existing.amount == amount
            ):
                return False
            raise
        wallet.balance += amount
        wallet.save(update_fields=["balance", "updated_at"])
        entry.balance_after = wallet.balance
        entry.save(update_fields=["balance_after"])
        return True


@transaction.atomic
def _settle_charge(operation, wallet, provider_cost):
    wallet = TokenWallet.objects.select_for_update().get(pk=wallet.pk)
    reservation = TokenLedgerEntry.objects.get(
        idempotency_key=f"reserve:{operation.id}"
    )
    if reservation.kind == TokenLedgerEntry.Kind.CHARGE:
        return abs(reservation.amount)
    charge = operation.reserved_tokens
    if provider_cost is not None and provider_cost > 0:
        conversion = Decimal(str(getattr(settings, "AI_CREDITS_PER_USD", 1000)))
        margin = Decimal(str(getattr(settings, "AI_COST_MARGIN_MULTIPLIER", 1.5)))
        minimum = int(getattr(settings, "AI_MIN_OPERATION_CHARGE", 1))
        estimated = int(
            (provider_cost * conversion * margin).to_integral_value(rounding=ROUND_CEILING)
        )
        charge = min(operation.reserved_tokens, max(minimum, estimated))
    refund_amount = operation.reserved_tokens - charge
    if refund_amount:
        key = f"settlement-refund:{operation.id}"
        if not TokenLedgerEntry.objects.filter(idempotency_key=key).exists():
            TokenLedgerEntry.objects.create(
                wallet=wallet,
                operation=operation,
                kind=TokenLedgerEntry.Kind.REFUND,
                amount=refund_amount,
                balance_after=wallet.balance + refund_amount,
                reason="Refund of unused estimated AI cost",
                idempotency_key=key,
            )
            wallet.balance += refund_amount
            wallet.save(update_fields=["balance", "updated_at"])
        reservation.amount = -charge
        reservation.balance_after = wallet.balance
        reservation.kind = TokenLedgerEntry.Kind.CHARGE
        reservation.reason = f"AI {operation.task_type} generation"
        reservation.save(update_fields=["amount", "balance_after", "kind", "reason"])
    else:
        reservation.kind = TokenLedgerEntry.Kind.CHARGE
        reservation.reason = f"AI {operation.task_type} generation"
        reservation.save(update_fields=["kind", "reason"])
    return charge


def _worker_attempt_limit():
    value = getattr(settings, "AI_WORKER_MAX_ATTEMPTS", 3)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PipelineError("AI_WORKER_MAX_ATTEMPTS must be a positive integer")
    return min(value, 10)


def _worker_lease_duration():
    timeout = getattr(settings, "OPENROUTER_TIMEOUT_SECONDS", 90)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError) as exc:
        raise PipelineError("OPENROUTER_TIMEOUT_SECONDS must be an integer") from exc
    configured = getattr(settings, "AI_WORKER_LEASE_SECONDS", 0)
    try:
        configured = int(configured)
    except (TypeError, ValueError) as exc:
        raise PipelineError("AI_WORKER_LEASE_SECONDS must be an integer") from exc
    return timedelta(seconds=max(300, timeout + 60, configured))


def _claim_operation(operation_id=None):
    now = timezone.now()
    retry_limit = _worker_attempt_limit()
    claimable = (
        Q(status=AIOperation.Status.QUEUED)
        | Q(
            status=AIOperation.Status.RUNNING,
            lease_expires_at__lte=now,
        )
        | Q(
            status=AIOperation.Status.RUNNING,
            lease_expires_at__isnull=True,
        )
    )
    candidates = AIOperation.objects.filter(
        claimable, worker_attempts__lt=retry_limit
    )
    if operation_id is not None:
        candidates = candidates.filter(pk=operation_id)
    candidate_id = candidates.order_by("created_at").values_list("pk", flat=True).first()
    if candidate_id is None:
        return None
    owner = uuid.uuid4()
    updated = AIOperation.objects.filter(
        claimable, pk=candidate_id, worker_attempts__lt=retry_limit
    ).update(
        status=AIOperation.Status.RUNNING,
        worker_attempts=F("worker_attempts") + 1,
        lease_owner=owner,
        lease_expires_at=now + _worker_lease_duration(),
        started_at=now,
        error="",
    )
    if not updated:
        return None
    AIProviderAttempt.objects.filter(
        operation_id=candidate_id,
        status=AIProviderAttempt.Status.RUNNING,
    ).update(
        status=AIProviderAttempt.Status.FAILED,
        is_retryable=True,
        error="Worker lease expired before provider attempt completed",
        completed_at=now,
    )
    return candidate_id, owner


def _mark_abandoned_provider_attempts(operation):
    AIProviderAttempt.objects.filter(
        operation=operation,
        status=AIProviderAttempt.Status.RUNNING,
    ).update(
        status=AIProviderAttempt.Status.FAILED,
        is_retryable=True,
        error="Worker lease expired before provider attempt completed",
        completed_at=timezone.now(),
    )


def _fail_operation(operation, error):
    _mark_abandoned_provider_attempts(operation)
    _refund(operation, "Refund after failed AI operation")
    operation.status = AIOperation.Status.FAILED
    operation.error = str(error)[:2000]
    operation.completed_at = timezone.now()
    operation.lease_owner = None
    operation.lease_expires_at = None
    operation.save(
        update_fields=[
            "status",
            "error",
            "completed_at",
            "lease_owner",
            "lease_expires_at",
            "provider_cost_usd",
        ]
    )
    pipeline = AIPipeline.objects.select_for_update().get(pk=operation.pipeline_id)
    pipeline.status = AIPipeline.Status.FAILED
    pipeline.last_error = operation.error
    pipeline.save(update_fields=["status", "last_error", "updated_at"])


def _finalize_exhausted_operations():
    retry_limit = _worker_attempt_limit()
    now = timezone.now()
    exhausted = (
        Q(status=AIOperation.Status.QUEUED)
        | Q(
            status=AIOperation.Status.RUNNING,
            lease_expires_at__lte=now,
        )
        | Q(
            status=AIOperation.Status.RUNNING,
            lease_expires_at__isnull=True,
        )
    )
    operation_ids = list(
        AIOperation.objects.filter(exhausted, worker_attempts__gte=retry_limit)
        .order_by("created_at")
        .values_list("pk", flat=True)
    )
    finalized = False
    for operation_id in operation_ids:
        with transaction.atomic():
            operation = AIOperation.objects.select_for_update().filter(
                pk=operation_id
            ).first()
            if not operation:
                continue
            expired = (
                operation.status == AIOperation.Status.QUEUED
                or operation.status == AIOperation.Status.RUNNING
                and (
                    operation.lease_expires_at is None
                    or operation.lease_expires_at <= now
                )
            )
            if expired and operation.worker_attempts >= retry_limit:
                operation.provider_cost_usd = _operation_provider_cost(operation)
                _fail_operation(operation, "AI operation exhausted its worker retry limit")
                finalized = True
    return finalized


def process_operation(operation_id, lease_owner=None):
    if lease_owner is None:
        claim = _claim_operation(operation_id)
        if claim is None:
            return False
        _, lease_owner = claim

    with transaction.atomic():
        operation = AIOperation.objects.select_related("pipeline", "user").filter(
            pk=operation_id,
            status=AIOperation.Status.RUNNING,
            lease_owner=lease_owner,
        ).first()
        if operation is None:
            return False

    try:
        outcome = _call_openrouter(operation)
        _validate_result(operation.task_type, outcome["result"])
        with transaction.atomic():
            operation = AIOperation.objects.select_for_update().get(pk=operation_id)
            if operation.status == AIOperation.Status.CANCELLED:
                operation.provider_cost_usd = _operation_provider_cost(
                    operation, outcome.get("provider_cost_usd")
                )
                operation.lease_owner = None
                operation.lease_expires_at = None
                operation.save(
                    update_fields=[
                        "provider_cost_usd",
                        "lease_owner",
                        "lease_expires_at",
                    ]
                )
                _refund(operation, "Cancelled AI operation")
                return True
            if operation.status != AIOperation.Status.RUNNING or operation.lease_owner != lease_owner:
                return False
            operation.result = outcome["result"]
            operation.model_name = outcome["model"]
            operation.input_tokens = outcome["input_tokens"]
            operation.output_tokens = outcome["output_tokens"]
            operation.provider_cost_usd = _operation_provider_cost(
                operation, outcome.get("provider_cost_usd")
            )
            wallet = TokenWallet.objects.select_for_update().get(user=operation.user)
            operation.charged_tokens = _settle_charge(
                operation, wallet, operation.provider_cost_usd
            )
            operation.status = AIOperation.Status.SUCCEEDED
            operation.completed_at = timezone.now()
            operation.lease_owner = None
            operation.lease_expires_at = None
            operation.save()
            TokenLedgerEntry.objects.filter(
                operation=operation, kind=TokenLedgerEntry.Kind.RESERVED
            ).update(kind=TokenLedgerEntry.Kind.CHARGE, reason=f"AI {operation.task_type} generation")
            _apply_result(operation, outcome["result"])
        return True
    except Exception as exc:
        logger.exception("AI operation %s failed", operation_id)
        with transaction.atomic():
            operation = AIOperation.objects.select_for_update().filter(pk=operation_id).first()
            if not operation:
                raise
            if operation.status == AIOperation.Status.CANCELLED:
                operation.provider_cost_usd = _operation_provider_cost(operation)
                operation.lease_owner = None
                operation.lease_expires_at = None
                operation.save(
                    update_fields=[
                        "provider_cost_usd",
                        "lease_owner",
                        "lease_expires_at",
                    ]
                )
                _refund(operation, "Cancelled AI operation")
                return True
            if operation.status != AIOperation.Status.RUNNING or operation.lease_owner != lease_owner:
                return False
            operation.provider_cost_usd = _operation_provider_cost(operation)
            if (
                isinstance(exc, RetryablePipelineError)
                and operation.worker_attempts < _worker_attempt_limit()
            ):
                operation.status = AIOperation.Status.QUEUED
                operation.error = str(exc)[:2000]
                operation.lease_owner = None
                operation.lease_expires_at = None
                operation.save(
                    update_fields=[
                        "status",
                        "error",
                        "lease_owner",
                        "lease_expires_at",
                        "provider_cost_usd",
                    ]
                )
                return True
            _fail_operation(operation, exc)
        return True


def run_worker_once():
    finalized = _finalize_exhausted_operations()
    claim = _claim_operation()
    if claim is None:
        return finalized
    operation_id, lease_owner = claim
    process_operation(operation_id, lease_owner)
    return True


def cancel_operation(operation):
    with transaction.atomic():
        locked = AIOperation.objects.select_for_update().get(pk=operation.pk)
        if locked.status == AIOperation.Status.QUEUED:
            _refund(locked, "Cancelled AI operation")
            locked.status = AIOperation.Status.CANCELLED
            locked.completed_at = timezone.now()
        elif locked.status == AIOperation.Status.RUNNING:
            _refund(locked, "Cancelled AI operation")
            AIProviderAttempt.objects.filter(
                operation=locked,
                status=AIProviderAttempt.Status.RUNNING,
            ).update(
                status=AIProviderAttempt.Status.FAILED,
                error="Operation cancelled while provider attempt was running",
                completed_at=timezone.now(),
            )
            locked.status = AIOperation.Status.CANCELLED
            locked.completed_at = timezone.now()
            locked.lease_owner = None
            locked.lease_expires_at = None
        else:
            return locked
        locked.save(
            update_fields=[
                "status",
                "completed_at",
                "lease_owner",
                "lease_expires_at",
            ]
        )
        pipeline = AIPipeline.objects.select_for_update().get(pk=locked.pipeline_id)
        pipeline.status = AIPipeline.Status.FAILED
        pipeline.last_error = "AI operation cancelled"
        pipeline.save(update_fields=["status", "last_error", "updated_at"])
        return locked


def estimate_operation(task_type):
    try:
        return operation_cost(task_type)
    except KeyError as exc:
        raise PipelineError("Unsupported AI task type") from exc
