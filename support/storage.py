from django.core.files.storage import storages


def support_attachment_storage():
    return storages["support_attachments"]
