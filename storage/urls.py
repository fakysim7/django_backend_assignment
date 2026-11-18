from django.urls import path

from .views import (
    StorageAdminDiagnosticsView,
    StorageFileDetailView,
    StorageFileDownloadUrlView,
    StorageFileDownloadView,
    StorageFilePreviewUrlView,
    StoragePreviewView,
    StorageProjectFilesView,
    StorageUploadConfirmView,
    StorageUploadContentView,
    StorageUploadInitializeView,
    StorageUploadUrlRefreshView,
)

urlpatterns = [
    path("uploads/", StorageUploadInitializeView.as_view(), name="storage-upload-init"),
    path(
        "uploads/<uuid:file_id>/content/",
        StorageUploadContentView.as_view(),
        name="storage-upload-content",
    ),
    path(
        "uploads/<uuid:file_id>/confirm/",
        StorageUploadConfirmView.as_view(),
        name="storage-upload-confirm",
    ),
    path(
        "uploads/<uuid:file_id>/signed-url/",
        StorageUploadUrlRefreshView.as_view(),
        name="storage-upload-url-refresh",
    ),
    path(
        "projects/<uuid:project_id>/files/",
        StorageProjectFilesView.as_view(),
        name="storage-project-files",
    ),
    path(
        "files/<uuid:file_id>/download-url/",
        StorageFileDownloadUrlView.as_view(),
        name="storage-file-download-url",
    ),
    path(
        "files/<uuid:file_id>/preview-url/",
        StorageFilePreviewUrlView.as_view(),
        name="storage-file-preview-url",
    ),
    path(
        "files/<uuid:file_id>/download/",
        StorageFileDownloadView.as_view(),
        name="storage-file-download",
    ),
    path(
        "files/<uuid:file_id>/preview/",
        StoragePreviewView.as_view(),
        name="storage-file-preview",
    ),
    path("files/<uuid:file_id>/", StorageFileDetailView.as_view(), name="storage-file-detail"),
    path("admin/diagnostics/", StorageAdminDiagnosticsView.as_view(), name="storage-admin-diagnostics"),
]
