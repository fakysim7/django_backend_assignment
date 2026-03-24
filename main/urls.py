from django.urls import path
from .views import register, login

urlpatterns = [
    path('api/register/', register),
    path('api/login/', login),
]