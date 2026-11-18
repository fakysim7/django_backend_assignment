from django.urls import path

from . import views

urlpatterns = [
    path("projects/", views.create_ai_project, name="ai-project-create"),
    path("projects/<uuid:project_id>/", views.pipeline_detail, name="ai-pipeline-detail"),
    path(
        "projects/<uuid:project_id>/operations/",
        views.start_operation,
        name="ai-operation-start",
    ),
    path(
        "projects/<uuid:project_id>/confirm/",
        views.confirm_stage,
        name="ai-stage-confirm",
    ),
    path(
        "projects/<uuid:project_id>/versions/<int:version_id>/restore/",
        views.restore_version,
        name="ai-version-restore",
    ),
    path("operations/<uuid:operation_id>/", views.operation_detail, name="ai-operation-detail"),
    path(
        "operations/<uuid:operation_id>/cancel/",
        views.cancel_ai_operation,
        name="ai-operation-cancel",
    ),
    path("account/", views.account_detail, name="ai-account"),
    path("estimate/", views.estimate, name="ai-operation-estimate"),
    path("admin/grants/", views.admin_grant, name="ai-admin-grant"),
]
