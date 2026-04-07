from rest_framework import serializers
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

