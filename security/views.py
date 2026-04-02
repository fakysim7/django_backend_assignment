from rest_framework_simplejwt.views import TokenRefreshView
from django.conf import settings
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.response import Response

# class CustomTokenObtainPairView(TokenObtainPairView):
#     def post(self, request, *args, **kwargs):
#         response = super().post(request, *args, **kwargs)
        
#         if response.status_code == 200:
#             refresh_token = response.data.get('refresh')
            
#             # Устанавливаем куку
#             response.set_cookie(
#                 key=settings.SIMPLE_JWT['AUTH_COOKIE'],
#                 value=refresh_token,
#                 expires=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'],
#                 httponly=settings.SIMPLE_JWT['AUTH_COOKIE_HTTP_ONLY'],
#                 secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE'],
#                 samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE'],
#                 path='/api/login/refresh/', # Кука будет летать только на этот эндпоинт
#             )
            
#             # Удаляем refresh из JSON ответа, чтобы фронт его не видел
#             del response.data['refresh']
            
#         return response
    

class CustomTokenRefreshView(TokenRefreshView):
    def post(self, request, *args, **kwargs):
        # Достаем токен из куки и подсовываем его в данные запроса
        refresh_token = request.COOKIES.get(settings.SIMPLE_JWT['AUTH_COOKIE'])
        
        if refresh_token:
            request.data['refresh'] = refresh_token
            
        return super().post(request, *args, **kwargs)