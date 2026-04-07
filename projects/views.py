from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from .serializers import ProjectSerializer
from django.core.files.storage import default_storage
from django.http import HttpResponse

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from main.models import Projects
from .services.word_generator import generate_docx_from_blocks
import uuid



class ProjectViewSet(ModelViewSet):
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # пользователь видит только свои проекты
        return Projects.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        # автоматически привязываем пользователя
        serializer.save(user=self.request.user)


@api_view(['POST'])
def upload_image(request):
    file = request.FILES.get('file')

    if not file:
        return Response({"error": "Файл не передан"}, status=400)

    file = validate_image(file)

    path = default_storage.save(f"uploads/{file.name}", file)
    url = request.build_absolute_uri(default_storage.url(path))

    return Response({"url": url})

def validate_image(file):
    if not file:
        raise ValueError("Файл отсутствует")

    if not file.content_type.startswith('image/'):
        raise ValueError("Только изображения")

    return file



@api_view(['GET'])
#@permission_classes([AllowAny])
@permission_classes([IsAuthenticated])
def download_project_docx(request, project_id):
    try:
        project = Projects.objects.get(id=project_id, user=request.user)
    except Projects.DoesNotExist:
        return Response({"error": "Проект не найден"}, status=404)

    doc = generate_docx_from_blocks(project.blocks)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )

    filename = f"{project.name}_{uuid.uuid4().hex}.docx"

    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    doc.save(response)

    return response