"""
URL configuration for sortirovschick project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from main.views import login, register, logout
from projects.views import ProjectViewSet, upload_image, download_project_docx
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from security.views import  CustomTokenRefreshView
from django.conf import settings
from django.conf.urls.static import static

router = DefaultRouter()
router.register(r'projects', ProjectViewSet, basename='projects')

# Swagger config
schema_view = get_schema_view(
    openapi.Info(
        title="My API",
        default_version='v1',
        description="Документация API",
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)

urlpatterns = [
    # auth
    path('api/register/', register),
    path('api/login/', login),
    path('api/logout/', logout),

    # refresh
    path('api/token/refresh/', CustomTokenRefreshView.as_view()),

    # projects
    path('api/', include(router.urls)),
    path('api/upload_image/', upload_image),

    # create .docx
    path('api/projects/<uuid:project_id>/download/', download_project_docx),

    # admin
    path('admin/', admin.site.urls),

    # swagger
    path('swagger/', schema_view.with_ui('swagger', cache_timeout=0)),
    path('redoc/', schema_view.with_ui('redoc', cache_timeout=0)),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)