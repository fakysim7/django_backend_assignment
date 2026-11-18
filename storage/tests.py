import hashlib
import io
import tempfile
from pathlib import Path
from PIL import Image
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
from io import StringIO
from django.core.management import call_command
from rest_framework.test import APIClient

from main.models import Projects, User
from support.models import Ticket, TicketAttachment, TicketMessage
from .models import StoredFile
from .providers import (
    LocalProvider,
    StorageConfigurationError,
    StorageError,
    TigrisProvider,
)
from .registry import get_provider, reset_provider_registry
from .services import (
    confirm_upload,
    delete_stored_file,
    initialize_upload,
    migrate_stored_file,
    upload_pending_file,
)

PNG_DATA = b"\x89PNG\r\n\x1a\n" + b"test image content"


class SecondaryLocalProvider(LocalProvider):
    name = "test-target"

    def __init__(self):
        super().__init__()
        self.root = (self.root / "target").resolve()
        self.root.mkdir(parents=True, exist_ok=True)


@override_settings(
    STORAGE_PROVIDER="local",
    STORAGE_ALLOW_LOCAL_FALLBACK=False,
    STORAGE_MAX_FILE_SIZE=1024 * 1024,
)
class StorageLifecycleTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(STORAGE_LOCAL_ROOT=self.temp_dir.name)
        self.settings_override.enable()
        reset_provider_registry()
        self.user = User.objects.create_user(
            username="storage-user",
            user_name="storage-user",
            email="storage@example.com",
            password="test-password",
        )

    def tearDown(self):
        reset_provider_registry()
        self.settings_override.disable()
        self.temp_dir.cleanup()

    def create_pending_file(self, **kwargs):
        return initialize_upload(
            owner=kwargs.pop("owner", self.user),
            filename=kwargs.pop("filename", "picture.png"),
            content_type=kwargs.pop("content_type", "image/png"),
            size=kwargs.pop("size", len(PNG_DATA)),
            **kwargs,
        )

    def test_local_upload_records_checksum_and_downloads_stream(self):
        stored_file = self.create_pending_file()
        result = upload_pending_file(
            stored_file,
            SimpleUploadedFile("picture.png", PNG_DATA, content_type="image/png"),
        )

        self.assertEqual(result.status, StoredFile.STATUS_UPLOADED)
        self.assertEqual(result.checksum, hashlib.sha256(PNG_DATA).hexdigest())
        with get_provider().download(result.storage_key) as downloaded:
            self.assertEqual(downloaded.read(), PNG_DATA)

    def test_repeated_upload_and_confirmation_are_idempotent(self):
        stored_file = self.create_pending_file()
        upload_pending_file(
            stored_file,
            SimpleUploadedFile("picture.png", PNG_DATA, content_type="image/png"),
        )
        upload_pending_file(
            stored_file,
            SimpleUploadedFile("picture.png", b"ignored", content_type="image/png"),
        )
        confirmed = confirm_upload(stored_file)
        self.assertEqual(confirm_upload(confirmed).checksum, hashlib.sha256(PNG_DATA).hexdigest())
        self.assertEqual(StoredFile.objects.count(), 1)

    def test_confirmation_checks_actual_object_and_signature(self):
        stored_file = self.create_pending_file()
        provider = get_provider()
        provider.upload(
            stored_file.storage_key,
            io.BytesIO(b"not a png"),
            content_type="image/png",
        )
        with self.assertRaises(Exception):
            confirm_upload(stored_file)
        stored_file.refresh_from_db()
        self.assertEqual(stored_file.status, StoredFile.STATUS_PENDING)

    def test_local_provider_rejects_path_traversal(self):
        with self.assertRaises(StorageError):
            get_provider()._path("../outside.txt")

    def test_upload_rejects_mime_extension_mismatch_and_oversize(self):
        with self.assertRaises(Exception):
            self.create_pending_file(content_type="text/plain")
        with self.assertRaises(Exception):
            self.create_pending_file(size=1024 * 1024 + 1)

    def test_delete_is_blocked_while_project_references_image(self):
        project = Projects.objects.create(
            user=self.user,
            name="Image project",
            type="course",
            status="active",
        )
        stored_file = self.create_pending_file(
            category="document_images",
            metadata={"project_id": str(project.id)},
        )
        upload_pending_file(
            stored_file,
            SimpleUploadedFile("picture.png", PNG_DATA, content_type="image/png"),
        )
        project.blocks = [
            {"type": "image", "content": {"storage_file_id": str(stored_file.id)}}
        ]
        project.save(update_fields=("blocks",))
        with self.assertRaises(ValueError):
            delete_stored_file(stored_file)

    def test_internal_support_attachment_is_not_visible_through_generic_api(self):
        other = User.objects.create_user(
            username="other-storage-user",
            user_name="other-storage-user",
            email="other-storage@example.com",
            password="test-password",
        )
        ticket = Ticket.objects.create(
            user=other,
            title="Private ticket",
            description="Details",
        )
        message = TicketMessage.objects.create(
            ticket=ticket,
            author=self.user,
            text="Internal",
            is_internal=True,
        )
        stored_file = self.create_pending_file(category="support_attachments")
        upload_pending_file(
            stored_file,
            SimpleUploadedFile("picture.png", PNG_DATA, content_type="image/png"),
        )
        TicketAttachment.objects.create(
            ticket=ticket,
            message=message,
            stored_file=stored_file,
            filename="picture.png",
            mime_type="image/png",
            size=len(PNG_DATA),
        )

        client = APIClient()
        client.force_authenticate(self.user)
        response = client.get(f"/api/storage/files/{stored_file.id}/")
        self.assertEqual(response.status_code, 403)

    def test_users_cannot_read_each_others_file_metadata(self):
        stored_file = self.create_pending_file()
        other = User.objects.create_user(
            username="metadata-user",
            user_name="metadata-user",
            email="metadata@example.com",
            password="test-password",
        )
        client = APIClient()
        client.force_authenticate(other)
        response = client.get(f"/api/storage/files/{stored_file.id}/")
        self.assertEqual(response.status_code, 403)

    def test_api_upload_content_and_confirmation(self):
        stored_file = self.create_pending_file()
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(
            f"/api/storage/uploads/{stored_file.id}/content/",
            {"file": SimpleUploadedFile("picture.png", PNG_DATA, content_type="image/png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], StoredFile.STATUS_UPLOADED)

    def test_api_confirmation_returns_updated_file_status(self):
        stored_file = self.create_pending_file()
        get_provider().upload(
            stored_file.storage_key,
            io.BytesIO(PNG_DATA),
            content_type="image/png",
        )
        client = APIClient()
        client.force_authenticate(self.user)

        response = client.post(
            f"/api/storage/uploads/{stored_file.id}/confirm/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], StoredFile.STATUS_UPLOADED)
        self.assertEqual(response.data["checksum"], hashlib.sha256(PNG_DATA).hexdigest())

        repeated = client.post(
            f"/api/storage/uploads/{stored_file.id}/confirm/",
            {},
            format="json",
        )
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(repeated.data["status"], StoredFile.STATUS_UPLOADED)

    def test_api_initialization_returns_backend_upload_endpoint_for_local_provider(self):
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(
            "/api/storage/uploads/",
            {
                "filename": "picture.png",
                "mime_type": "image/png",
                "file_size": len(PNG_DATA),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["upload_url"])
        self.assertEqual(response.data["upload_method"], "POST")
        self.assertIn(response.data["id"], response.data["upload_endpoint"])
        refreshed = client.post(
            f"/api/storage/uploads/{response.data['id']}/signed-url/"
        )
        self.assertEqual(refreshed.status_code, 200)
        self.assertIsNone(refreshed.data["upload_url"])
        self.assertIn(response.data["id"], refreshed.data["upload_endpoint"])

    def test_document_image_upload_is_project_scoped_and_preview_is_temporary(self):
        project = Projects.objects.create(
            user=self.user,
            name="Image upload",
            type="course",
            status="active",
        )
        image_buffer = io.BytesIO()
        Image.new("RGB", (2, 2), color="blue").save(image_buffer, format="PNG")
        image_buffer.seek(0)
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(
            "/api/upload_image/",
            {
                "file": SimpleUploadedFile(
                    "cover.png",
                    image_buffer.read(),
                    content_type="image/png",
                ),
                "project_id": str(project.id),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        stored_file = StoredFile.objects.get(id=response.data["storage_file_id"])
        self.assertEqual(stored_file.category, "document_images")
        self.assertEqual(stored_file.metadata["project_id"], str(project.id))
        self.assertNotIn("cover", stored_file.storage_key)

        preview = client.get(
            f"/api/storage/files/{stored_file.id}/preview-url/"
        )
        self.assertEqual(preview.status_code, 200)
        image = client.get(f"/api/{preview.data['url']}")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image["Content-Type"], "image/png")
        image.close()
        other = User.objects.create_user(
            username="foreign-image-user",
            user_name="foreign-image-user",
            email="foreign-image@example.com",
            password="test-password",
        )
        foreign_client = APIClient()
        foreign_client.force_authenticate(other)
        self.assertEqual(
            foreign_client.get(
                f"/api/storage/files/{stored_file.id}/preview-url/"
            ).status_code,
            403,
        )

    def test_migration_verifies_checksum_and_preserves_source_object(self):
        stored_file = self.create_pending_file()
        stored_file = upload_pending_file(
            stored_file,
            SimpleUploadedFile("picture.png", PNG_DATA, content_type="image/png"),
        )
        source = get_provider()
        source_key = stored_file.storage_key
        with patch.dict("storage.registry._PROVIDERS", {"test-target": SecondaryLocalProvider}):
            reset_provider_registry()
            migration = migrate_stored_file(stored_file, "test-target")
            self.assertEqual(migration.status, "completed")
            self.assertEqual(migration.checksum, stored_file.checksum)
            self.assertTrue(source.exists(source_key))
            stored_file.refresh_from_db()
            self.assertEqual(stored_file.provider, "test-target")
            self.assertEqual(get_provider("test-target").checksum(source_key), stored_file.checksum)

    def test_cleanup_defaults_to_dry_run(self):
        stored_file = self.create_pending_file()
        stored_file.status = StoredFile.STATUS_FAILED
        stored_file.save(update_fields=("status", "updated_at"))
        StoredFile.objects.filter(pk=stored_file.pk).update(
            updated_at=timezone.now() - timedelta(days=2)
        )
        output = StringIO()
        call_command(
            "cleanup_stored_files",
            "--older-than-hours=1",
            stdout=output,
        )
        stored_file.refresh_from_db()
        self.assertEqual(stored_file.status, StoredFile.STATUS_FAILED)
        self.assertIn("would clean", output.getvalue())


class TigrisProviderTests(TestCase):
    @override_settings(TIGRIS_ACCESS_KEY_ID="", TIGRIS_SECRET_ACCESS_KEY="")
    def test_missing_credentials_fail_initialization(self):
        with self.assertRaises(StorageConfigurationError):
            TigrisProvider()

    def test_explicit_fallback_only_applies_when_credentials_are_missing(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(
                STORAGE_PROVIDER="tigris",
                STORAGE_ALLOW_LOCAL_FALLBACK=True,
                STORAGE_LOCAL_ROOT=root,
                TIGRIS_ACCESS_KEY_ID="",
                TIGRIS_SECRET_ACCESS_KEY="",
            ):
                reset_provider_registry()
                self.assertEqual(get_provider().name, "local")
                with self.assertRaises(StorageConfigurationError):
                    get_provider("tigris")
                reset_provider_registry()

    def test_runtime_tigris_error_is_not_replaced_by_local_provider(self):
        class RuntimeFailure:
            def __init__(self):
                raise StorageError("cloud unavailable")

        with tempfile.TemporaryDirectory() as root:
            with override_settings(
                STORAGE_PROVIDER="tigris",
                STORAGE_ALLOW_LOCAL_FALLBACK=True,
                STORAGE_LOCAL_ROOT=root,
                TIGRIS_ACCESS_KEY_ID="configured",
                TIGRIS_SECRET_ACCESS_KEY="configured",
            ):
                with patch.dict(
                    "storage.registry._PROVIDERS",
                    {"tigris": RuntimeFailure},
                ):
                    reset_provider_registry()
                    with self.assertRaises(StorageError):
                        get_provider()
                    reset_provider_registry()

    @override_settings(
        TIGRIS_ACCESS_KEY_ID="test-access",
        TIGRIS_SECRET_ACCESS_KEY="test-secret",
        TIGRIS_BUCKET="test-bucket",
        TIGRIS_ENDPOINT="https://t3.storage.dev",
        TIGRIS_REGION="auto",
    )
    @patch("storage.providers.boto3.client")
    def test_signed_url_does_not_expose_credentials(self, client_factory):
        client = Mock()
        client.generate_presigned_url.return_value = "https://signed.example/object"
        client_factory.return_value = client
        url = TigrisProvider().generate_signed_upload_url(
            "objects/key.png",
            content_type="image/png",
            expires_in=600,
            content_length=len(PNG_DATA),
        )
        self.assertEqual(url, "https://signed.example/object")
        self.assertNotIn("test-secret", url)
        self.assertEqual(
            client.generate_presigned_url.call_args.kwargs["Params"]["ContentLength"],
            len(PNG_DATA),
        )


class StorageAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="api-storage-user",
            user_name="api-storage-user",
            email="api-storage@example.com",
            password="test-password",
        )

    def test_private_file_defaults_to_private_visibility(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(
                STORAGE_PROVIDER="local",
                STORAGE_LOCAL_ROOT=root,
                STORAGE_MAX_FILE_SIZE=1024,
                STORAGE_ALLOW_LOCAL_FALLBACK=False,
            ):
                reset_provider_registry()
                stored_file = initialize_upload(
                    owner=self.user,
                    filename="note.txt",
                    content_type="text/plain",
                    size=5,
                )
                self.assertEqual(
                    stored_file.visibility,
                    StoredFile.VISIBILITY_PRIVATE,
                )
                reset_provider_registry()
