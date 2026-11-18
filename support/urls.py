from django.urls import path

from .views import (
    SupportAdminTicketListView,
    SupportAttachmentDownloadView,
    AdminSupportAttachmentDownloadView,
    SupportAttachmentUploadView,
    AdminTicketMessageCreateView,
    SupportAdminTicketDetailView,
    SupportTicketAssignmentView,
    SupportTicketNoteView,
    SupportTicketStatusView,
    SupportAssigneeListView,
    TicketDetailView,
    TicketListCreateView,
    TicketMessageCreateView,
    SupportCategoryListView,
)

urlpatterns = [
    path("support/categories/", SupportCategoryListView.as_view(), name="support-categories"),
    path("support/tickets/", TicketListCreateView.as_view(), name="support-ticket-list-create"),
    path("support/tickets/<uuid:ticket_id>/", TicketDetailView.as_view(), name="support-ticket-detail"),
    path("support/tickets/<uuid:ticket_id>/messages/", TicketMessageCreateView.as_view(), name="support-ticket-message"),
    path("support/tickets/<uuid:ticket_id>/attachments/", SupportAttachmentUploadView.as_view(), name="support-ticket-attachment-upload"),
    path("support/tickets/<uuid:ticket_id>/attachments/<uuid:attachment_id>/", SupportAttachmentDownloadView.as_view(), name="support-ticket-attachment-download"),
    path("admin/support/tickets/", SupportAdminTicketListView.as_view(), name="admin-support-ticket-list"),
    path("admin/support/tickets/<uuid:ticket_id>/", SupportAdminTicketDetailView.as_view(), name="admin-support-ticket-detail"),
    path("admin/support/tickets/<uuid:ticket_id>/attachments/<uuid:attachment_id>/", AdminSupportAttachmentDownloadView.as_view(), name="admin-support-ticket-attachment-download"),
    path("admin/support/tickets/<uuid:ticket_id>/status/", SupportTicketStatusView.as_view(), name="admin-support-ticket-status"),
    path("admin/support/tickets/<uuid:ticket_id>/assignment/", SupportTicketAssignmentView.as_view(), name="admin-support-ticket-assignment"),
    path("admin/support/tickets/<uuid:ticket_id>/notes/", SupportTicketNoteView.as_view(), name="admin-support-ticket-note"),
    path("admin/support/tickets/<uuid:ticket_id>/messages/", AdminTicketMessageCreateView.as_view(), name="admin-support-ticket-message"),
    path("admin/support/assignees/", SupportAssigneeListView.as_view(), name="admin-support-assignees"),
]
