import secrets
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import GoogleAuthSerializer, RegisterSerializer

User = get_user_model()


def authenticate_user(login_input: str, password: str):
    """
    Аутентификация пользователя по user_name или email.
    """
    try:
        user = User.objects.get(user_name=login_input)
    except User.DoesNotExist:
        try:
            user = User.objects.get(email=login_input)
        except User.DoesNotExist:
            return None

    if user.check_password(password):
        return user
    return None


def set_jwt_cookies_and_response(user, status_code=status.HTTP_200_OK):
    """
    Формирует единый формат ответа с токенами и HttpOnly cookie для приложения.
    """
    refresh = RefreshToken.for_user(user)
    access = str(refresh.access_token)

    response = Response({
        "access": access,
        "user": {
            "id": str(user.user_id),
            "user_name": user.user_name,
            "email": user.email,
        }
    }, status=status_code)

    response.set_cookie(
        key=settings.SIMPLE_JWT['AUTH_COOKIE'],
        value=str(refresh),
        httponly=True,
        secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE'],
        samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE'],
        path=settings.SIMPLE_JWT['AUTH_COOKIE_PATH'],
    )
    return response


@api_view(['POST'])
@permission_classes([AllowAny])
def register(request):
    serializer = RegisterSerializer(data=request.data)

    if serializer.is_valid():
        user = serializer.save()
        return set_jwt_cookies_and_response(user, status_code=status.HTTP_201_CREATED)

    return Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([AllowAny])
def login(request):
    login_input = request.data.get('login')
    password = request.data.get('password')

    user = authenticate_user(login_input, password)

    if user is None:
        return Response({
            "errors": {
                "non_field_errors": ["Неверный логин или пароль"]
            }
        }, status=status.HTTP_400_BAD_REQUEST)

    return set_jwt_cookies_and_response(user, status_code=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def google_auth(request):
    serializer = GoogleAuthSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

    # Достаем verified id_info из сериализатора
    id_info = serializer.validated_data['id_info']
    email = id_info.get("email")

    if not email:
        return Response({"errors": {"email": ["Email не получен от Google"]}}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        # Извлекаем имя из Google или берём префикс email
        google_name = id_info.get("name") or id_info.get("given_name")
        base_username = google_name if google_name else email.split('@')[0]
        
        # Очищаем имя от спецсимволов для безопасного username (опционально)
        base_username = "".join(c for c in base_username if c.isalnum() or c in ("_", "-")).strip() or "user"
        
        user_name = base_username

        counter = 1
        while User.objects.filter(user_name=user_name).exists():
            user_name = f"{base_username}_{counter}"
            counter += 1

        user = User.objects.create(
            email=email,
            user_name=user_name,
            username=user_name
        )
        user.set_password(secrets.token_urlsafe(32))
        user.save()

    # Формируем ответ с токенами и cookies
    response = set_jwt_cookies_and_response(user, status_code=status.HTTP_200_OK)

    # Добавляем плоские поля user_id, user_name и email на верхний уровень JSON для фронтенда
    response.data.update({
        "user_id": str(user.user_id),
        "user_name": user.user_name,
        "email": user.email,
    })

    return response

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    """
    Возвращает данные текущего аутентифицированного пользователя по Access токену.
    """
    user = request.user
    return Response({
        "id": str(user.user_id),
        "user_name": user.user_name,
        "email": user.email,
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def profile(request):
    return Response({
        'username': request.user.user_name,
        'email': request.user.email
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def logout(request):
    response = Response({"message": "Logged out"}, status=status.HTTP_200_OK)
    response.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE'])
    return response