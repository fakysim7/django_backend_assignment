import requests
from django.conf import settings
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed

from .models import User


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ['user_name', 'email', 'password']

    def create(self, validated_data):
        user_name = validated_data['user_name']
        email = validated_data['email']
        password = validated_data['password']

        user = User(
            user_name=user_name,
            email=email,
            username=user_name 
        )
        user.set_password(password)
        user.save()
        return user

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("Email уже зарегистрирован")
        return value

    def validate_user_name(self, value):
        if User.objects.filter(user_name=value).exists():
            raise serializers.ValidationError("Имя пользователя уже занято")
        return value


class GoogleAuthSerializer(serializers.Serializer):
    code = serializers.CharField(required=True, write_only=True)

    def validate(self, attrs):
        code = attrs.get("code")
        token_endpoint = "https://oauth2.googleapis.com/token"
        data = {
            "code": code,
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        }

        # 1. Обмениваем authorization_code на токены в Google
        try:
            response = requests.post(token_endpoint, data=data, timeout=10)
        except requests.RequestException:
            raise AuthenticationFailed("Не удалось связаться с серверами Google.")

        if response.status_code != 200:
            raise AuthenticationFailed("Неудачный обмен кода на токены в Google.")

        tokens = response.json()
        raw_id_token = tokens.get("id_token")

        if not raw_id_token:
            raise AuthenticationFailed("ID токен отсутствует в ответе Google.")

        # 2. Проверяем подпись и целостность id_token
        try:
            id_info = id_token.verify_oauth2_token(
                raw_id_token,
                google_requests.Request(),
                settings.GOOGLE_OAUTH_CLIENT_ID,
            )
        except ValueError:
            raise AuthenticationFailed("Невалидный Google ID Token.")

        if not id_info.get("email_verified"):
            raise AuthenticationFailed("Email не подтвержден в Google.")

        # Сохраняем расшифрованные данные профиля в провалидированные атрибуты
        attrs["id_info"] = id_info
        return attrs