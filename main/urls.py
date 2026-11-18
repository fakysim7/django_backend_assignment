from django.urls import path
from .views import register, login, logout, profile, google_auth

urlpatterns = [
    path('register/', register, name='register'),
    path('login/', login, name='login'),
    path('logout/', logout, name='logout'),
    path('profile/', profile, name='profile'),
    path('google/', google_auth, name='google_auth'),
]