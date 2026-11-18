import os
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from .serializers import ProjectSerializer
from django.http import HttpResponse

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from main.models import Projects
from .services.word_generator import generate_docx_from_blocks
import uuid

from io import BytesIO
from PIL import Image, ImageOps
from django.core.files.base import ContentFile
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError
from storage.models import StoredFile
from storage.providers import StorageError
from storage.services import initialize_upload, upload_pending_file
from storage.views import preview_url_for



ALLOWED_IMAGE_TYPES = {
    'jpeg': 'image/jpeg',
    'jpg': 'image/jpeg',
    'png': 'image/png',
    'webp': 'image/webp',
    'gif': 'image/gif'
}

class ProjectViewSet(ModelViewSet):
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # пользователь видит только свои проекты
        return Projects.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)



MAX_FILE_SIZE = 10 * 1024 * 1024  


def validate_and_sanitize_image(uploaded_file):
    """
    Проверяет файл на размер, расширение, структуру Pillow 
    и пересобирает изображение для удаления скрытых угроз.
    """
    # 1. Проверка размера файла
    if uploaded_file.size > MAX_FILE_SIZE:
        raise ValueError("Размер файла превышает допустимый лимит (10 МБ)")

    # 2. Проверка расширения из оригинального имени
    ext = os.path.splitext(uploaded_file.name)[1].lower().lstrip('.')
    if ext not in ALLOWED_IMAGE_TYPES:
        raise ValueError(f"Неподдерживаемый формат файла .{ext}")

    try:
        # 3. Проверка Pillow: открытие и проверка структуры (Magic Bytes)
        img = Image.open(uploaded_file)
        img.verify()  

        uploaded_file.seek(0)
        img = Image.open(uploaded_file)
        img_format = img.format if img.format else 'JPEG'

        # Исправляем ориентацию EXIF (если фото снято на телефон)
        img = ImageOps.exif_transpose(img)

        # 4. Пересборка файла в памяти (удаляет скрытые в EXIF/метаданных скрипты)
        buffer = BytesIO()
        
        # Переконвертируем RGBA в RGB, если сохраняем как JPEG
        if img_format.upper() in ['JPEG', 'JPG'] and img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')

        img.save(buffer, format=img_format, quality=90, optimize=True)
        buffer.seek(0)

        # Возвращаем очищенный ContentFile и валидное расширение
        sanitized_file = ContentFile(buffer.read())
        return sanitized_file, ext.lower()

    except Exception as e:
        raise ValueError("Загруженный файл не является валидным изображением или поврежден")


@api_view(['POST'])
@permission_classes([IsAuthenticated])  # Рекомендуется закрыть авторизацией
def upload_image(request):
    file = request.FILES.get('file')

    if not file:
        return Response({"error": "Файл не передан"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Валидируем и получаем очищенный бинарник файла
        sanitized_file, ext = validate_and_sanitize_image(file)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    if not request.data.get("project_id"):
        return Response(
            {"error": "Для изображения необходимо указать проект."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        project = Projects.objects.get(
            id=request.data["project_id"],
            user=request.user,
        )
    except (Projects.DoesNotExist, ValueError):
        return Response({"error": "Проект не найден."}, status=status.HTTP_404_NOT_FOUND)

    content_type = ALLOWED_IMAGE_TYPES[ext]
    sanitized_file.name = f"image.{ext}"
    sanitized_file.seek(0, os.SEEK_END)
    sanitized_size = sanitized_file.tell()
    sanitized_file.seek(0)
    try:
        stored_file = initialize_upload(
            owner=request.user,
            filename=file.name,
            content_type=content_type,
            size=sanitized_size,
            category="document_images",
            metadata={"project_id": str(project.id)},
        )
        stored_file = upload_pending_file(stored_file, sanitized_file)
    except (DRFValidationError, ValueError) as error:
        if "stored_file" in locals():
            stored_file.status = StoredFile.STATUS_FAILED
            stored_file.save(update_fields=("status", "updated_at"))
        return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)
    except StorageError as error:
        return Response(
            {"error": "Хранилище файлов временно недоступно."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(
        {
            "id": str(stored_file.id),
            "storage_file_id": str(stored_file.id),
            "url": preview_url_for(stored_file),
            "path": "",
        },
        status=status.HTTP_201_CREATED,
    )



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_project_docx(request, project_id):
    try:
        project = Projects.objects.get(id=project_id, user=request.user)
    except Projects.DoesNotExist:
        return Response({"error": "Проект не найден"}, status=404)

   
    try:
        doc = generate_docx_from_blocks(
            project.blocks,
            project.type,
            owner=project.user,
            project_id=project.id,
        )
    except StorageError:
        return Response(
            {"error": "Хранилище файлов временно недоступно."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )

    filename = f"{project.name}_{uuid.uuid4().hex}.docx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    doc.save(response)
    return response