from platform_admin.permissions import HasPlatformPermission


class CanViewSupportQueue(HasPlatformPermission):
    permission_codename = "view_support_queue"


class CanViewSupportTicket(HasPlatformPermission):
    permission_codename = "view_support_tickets"


class CanManageSupportTickets(HasPlatformPermission):
    permission_codename = "manage_support_tickets"


class CanReplySupportTickets(HasPlatformPermission):
    permission_codename = "reply_support_tickets"


class CanAssignSupportTickets(HasPlatformPermission):
    permission_codename = "assign_support_tickets"


class CanAddInternalSupportNotes(HasPlatformPermission):
    permission_codename = "add_internal_support_notes"
