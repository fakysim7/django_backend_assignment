from rest_framework import serializers
from .models import User, Projects

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
    

class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Projects
        fields = '__all__'
        read_only_fields = ['user']