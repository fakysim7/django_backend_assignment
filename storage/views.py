from django.core import signing
from django.core.cache import cache
from django.conf import settings
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from platform_admin.authentication import PlatformAdminAPIView
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from main.models import Projects
from .models import StoredFile
from .providers import StorageError
from .services import (
    confirm_upload,
    delete_stored_file,
    download_stored_file,
    generate_download_url,
    generate_upload_url,
    initialize_upload,
    storage_diagnostics,
    upload_pending_file,
)


class StorageUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Хранилище файлов временно недоступно."
    default_code = "storage_unavailable"


def _project_for_user(user, project_id):
    return get_object_or_404(Projects, id=project_id, user=user)


def _can_access_file(user, stored_file):
    if stored_file.category == "support_attachments":
        attachments = stored_file.ticket_attachments.select_related(
            "ticket", "message"
        )
        return any(
            (not attachment.message_id or not attachment.message.is_internal)
            and (
                attachment.ticket.user_id == user.pk
                or user.is_superuser
                or user.has_perm("platform_admin.view_support_tickets")
            )
            for attachment in attachments
        )
    if stored_file.category == "document_images":
        project_id = stored_file.metadata.get("project_id")
        return bool(
            stored_file.owner_id == user.pk
            and project_id
            and Projects.objects.filter(id=project_id, user=user).exists()
        )
    return stored_file.owner_id == user.pk


def _get_owned_file(request, file_id):
    stored_file = get_object_or_404(StoredFile, id=file_id)
    if not _can_access_file(request.user, stored_file):
        raise PermissionDenied("Нет доступа к этому файлу.")
    return stored_file


def _validate_document_image_project(request, stored_file):
    project_id = stored_file.metadata.get("project_id")
    if not project_id:
        raise PermissionDenied("Файл не связан с проектом.")
    _project_for_user(request.user, project_id)


def _serialize_file(stored_file):
    return {
        "id": str(stored_file.id),
        "original_filename": stored_file.original_filename,
        "filename": stored_file.normalized_filename,
        "mime_type": stored_file.mime_type,
        "size": stored_file.file_size,
        "checksum": stored_file.checksum,
        "category": stored_file.category,
        "visibility": stored_file.visibility,
        "status": stored_file.status,
        "created_at": stored_file.created_at,
        "uploaded_at": stored_file.uploaded_at,
    }


def _initialize(request, data):
    category = data.get("category", "other")
    metadata = {}
    if category == "document_images":
        project_id = data.get("project_id")
        if not project_id:
            raise ValidationError({"project_id": "Для изображения укажите проект."})
        _project_for_user(request.user, project_id)
        metadata["project_id"] = str(project_id)
    elif category != "other":
        raise ValidationError({"category": "Для этой категории нет доступного upload flow."})

    stored_file = None
    try:
        stored_file = initialize_upload(
            owner=request.user,
            filename=data.get("filename", ""),
            content_type=data.get("mime_type", ""),
            size=data.get("file_size"),
            category=category,
            visibility=StoredFile.VISIBILITY_PRIVATE,
            metadata=metadata,
        )
        signed_url = generate_upload_url(stored_file)
    except StorageError as error:
        if stored_file:
            stored_file.status = StoredFile.STATUS_FAILED
            stored_file.save(update_fields=("status", "updated_at"))
        raise StorageUnavailable() from error
    return stored_file, signed_url


class StorageUploadInitializeView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        stored_file, signed_url = _initialize(request, request.data)
        return Response(
            {
                **_serialize_file(stored_file),
                "upload_url": signed_url,
                "upload_method": "PUT" if signed_url else "POST",
                "upload_endpoint": (
                    None
                    if signed_url
                    else f"storage/uploads/{stored_file.id}/content/"
                ),
                "expires_in": None if not signed_url else settings.STORAGE_SIGNED_URL_TTL,
            },
            status=status.HTTP_201_CREATED,
        )


class StorageUploadContentView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, file_id):
        stored_file = get_object_or_404(
            StoredFile,
            id=file_id,
            owner=request.user,
            status=StoredFile.STATUS_PENDING,
        )
        uploaded = request.FILES.get("file")
        if not uploaded:
            raise ValidationError({"file": "Файл обязателен."})
        if stored_file.category == "document_images":
            _validate_document_image_project(request, stored_file)
        if uploaded.size != stored_file.file_size:
            raise ValidationError({"file": "Размер файла не совпадает с заявленным."})
        try:
            stored_file = upload_pending_file(stored_file, uploaded)
        except (ValueError, ValidationError) as error:
            stored_file.status = StoredFile.STATUS_FAILED
            stored_file.save(update_fields=("status", "updated_at"))
            raise ValidationError(str(error)) from error
        except StorageError as error:
            raise StorageUnavailable() from error
        return Response(_serialize_file(stored_file), status=status.HTTP_201_CREATED)


class StorageUploadConfirmView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, file_id):
        stored_file = get_object_or_404(
            StoredFile,
            id=file_id,
            owner=request.user,
        )
        if stored_file.category == "document_images":
            _validate_document_image_project(request, stored_file)
        try:
            stored_file = confirm_upload(stored_file)
        except FileNotFoundError as error:
            raise ValidationError({"detail": "Объект не найден в хранилище."}) from error
        except (ValueError, ValidationError) as error:
            stored_file.status = StoredFile.STATUS_FAILED
            stored_file.save(update_fields=("status", "updated_at"))
            raise ValidationError(str(error)) from error
        except StorageError as error:
            raise StorageUnavailable() from error
        return Response(_serialize_file(stored_file))


class StorageUploadUrlRefreshView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, file_id):
        stored_file = get_object_or_404(
            StoredFile,
            id=file_id,
            owner=request.user,
            status=StoredFile.STATUS_PENDING,
        )
        try:
            signed_url = generate_upload_url(stored_file)
        except StorageError as error:
            raise StorageUnavailable() from error
        return Response(
            {
                "upload_url": signed_url,
                "upload_method": "PUT" if signed_url else "POST",
                "upload_endpoint": (
                    None
                    if signed_url
                    else f"storage/uploads/{stored_file.id}/content/"
                ),
                "expires_in": (
                    None if not signed_url else settings.STORAGE_SIGNED_URL_TTL
                ),
            }
        )


class StorageFileDetailView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, file_id):
        stored_file = _get_owned_file(request, file_id)
        return Response(_serialize_file(stored_file))

    def delete(self, request, file_id):
        stored_file = _get_owned_file(request, file_id)
        try:
            delete_stored_file(stored_file)
        except ValueError as error:
            raise ValidationError(str(error)) from error
        except StorageError as error:
            raise StorageUnavailable() from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class StorageFileDownloadView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, file_id):
        stored_file = _get_owned_file(request, file_id)
        if stored_file.status != StoredFile.STATUS_UPLOADED:
            raise Http404
        try:
            signed_url = generate_download_url(stored_file)
            if signed_url:
                return HttpResponseRedirect(signed_url)
            return FileResponse(
                download_stored_file(stored_file),
                as_attachment=True,
                filename=stored_file.normalized_filename,
                content_type=stored_file.mime_type,
            )
        except StorageError as error:
            raise StorageUnavailable() from error


class StorageFileDownloadUrlView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, file_id):
        stored_file = _get_owned_file(request, file_id)
        if stored_file.status != StoredFile.STATUS_UPLOADED:
            raise Http404
        try:
            signed_url = generate_download_url(stored_file)
        except StorageError as error:
            raise StorageUnavailable() from error
        if signed_url:
            return Response({"url": signed_url, "expires_in": settings.STORAGE_SIGNED_URL_TTL})
        return Response(
            {
                "url": f"/api/storage/files/{stored_file.id}/download/",
                "expires_in": None,
            }
        )


class StorageFilePreviewUrlView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, file_id):
        stored_file = _get_owned_file(request, file_id)
        if (
            stored_file.category != "document_images"
            or stored_file.status != StoredFile.STATUS_UPLOADED
        ):
            raise Http404
        return Response(
            {
                "url": preview_url_for(stored_file),
                "expires_in": settings.STORAGE_SIGNED_URL_TTL,
            }
        )


class StorageProjectFilesView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, project_id):
        project = _project_for_user(request.user, project_id)
        files = StoredFile.objects.filter(
            owner=request.user,
            category="document_images",
            metadata__project_id=str(project.id),
            status=StoredFile.STATUS_UPLOADED,
        ).order_by("-created_at")
        return Response([_serialize_file(file) for file in files])


class StoragePreviewView(APIView):
    permission_classes = ()
    authentication_classes = ()

    def get(self, request, file_id):
        token = request.query_params.get("token", "")
        try:
            payload = signing.loads(
                token,
                salt="storage.file-preview",
                max_age=settings.STORAGE_SIGNED_URL_TTL,
            )
        except signing.BadSignature as error:
            raise Http404 from error
        if payload.get("file_id") != str(file_id):
            raise Http404
        stored_file = get_object_or_404(
            StoredFile,
            id=file_id,
            status=StoredFile.STATUS_UPLOADED,
            category="document_images",
        )
        try:
            return FileResponse(
                download_stored_file(stored_file),
                content_type=stored_file.mime_type,
            )
        except StorageError as error:
            raise StorageUnavailable() from error


class StorageAdminDiagnosticsView(PlatformAdminAPIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        if not (
            request.user.is_superuser
            or request.user.has_perm("platform_admin.manage_admin_roles")
        ):
            raise PermissionDenied("Недостаточно прав для диагностики Storage.")
        cached = cache.get("storage_admin_diagnostics")
        if cached is None:
            cached = storage_diagnostics()
            cache.set("storage_admin_diagnostics", cached, timeout=300)
        return Response(cached)


def preview_url_for(stored_file):
    token = signing.dumps(
        {"file_id": str(stored_file.id)},
        salt="storage.file-preview",
    )
    return f"storage/files/{stored_file.id}/preview/?token={token}"
