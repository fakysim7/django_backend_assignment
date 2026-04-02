from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from rest_framework.viewsets import ModelViewSet
from .serializers import RegisterSerializer, ProjectSerializer
from django.contrib.auth import authenticate
from rest_framework_simplejwt.tokens import RefreshToken
from django.conf import settings

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from .models import Projects

from django.contrib.auth import get_user_model

User = get_user_model()

def authenticate_user(login: str, password: str):
    """
    login — это либо user_name, либо email
    """
    try:
        # сначала ищем по user_name
        user = User.objects.get(user_name=login)
    except User.DoesNotExist:
        try:
            # если не нашли — пробуем по email
            user = User.objects.get(email=login)
        except User.DoesNotExist:
            return None

    # проверяем пароль
    if user.check_password(password):
        return user
    return None


@api_view(['POST'])
def register(request):
    serializer = RegisterSerializer(data=request.data)

    if serializer.is_valid():
        user = serializer.save()

        refresh = RefreshToken.for_user(user)
        access = str(refresh.access_token)

        response = Response({
            "user": {
                "id": str(user.user_id),
                "user_name": user.user_name,
                "email": user.email
            },
            "access": access
        }, status=201)

        # refresh -> cookie
        response.set_cookie(
            key=settings.SIMPLE_JWT['AUTH_COOKIE'],
            value=str(refresh),
            httponly=True,
            secure=False,  # True в проде не забыть
            samesite='Lax',
            path='/api/token/refresh/'
        )

        return response

    return Response({"errors": serializer.errors}, status=400)


@api_view(['POST'])
def login(request):
    login_input = request.data.get('login')
    password = request.data.get('password')

    user = authenticate_user(login_input, password)

    if user is None:
        return Response({
            "errors": {
                "non_field_errors": ["Неверный логин или пароль"]
            }
        }, status=400)

    refresh = RefreshToken.for_user(user)
    access = str(refresh.access_token)

    response = Response({
        "access": access,
        "user": {
            "user_name": user.user_name,
            "email": user.email,
        }
    })

    # refresh -> cookie
    response.set_cookie(
        key=settings.SIMPLE_JWT['AUTH_COOKIE'],
        value=str(refresh),
        httponly=True,
        secure=False,  # True в проде не забыть
        samesite='Lax',
        path='/api/token/refresh/'
    )

    return response


class ProjectViewSet(ModelViewSet):
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # пользователь видит только свои проекты
        return Projects.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        # автоматически привязываем пользователя
        serializer.save(user=self.request.user)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def profile(request):
    return Response({
        'username': request.user.user_name,
        'email': request.user.email
    })

@api_view(['POST'])
def logout(request):
    response = Response({"message": "Logged out"})
    response.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE'])
    return response