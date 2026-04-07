from rest_framework import serializers
from main.models import Projects

class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Projects
        fields = '__all__'
        read_only_fields = ['user']