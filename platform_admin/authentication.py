from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.sessions.backends.db import SessionStore
from django.middleware.csrf import CsrfViewMiddleware, get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import PlatformAdminAccount

ADMIN_SESSION_KEY = "platform_admin_account_id"
ADMIN_SESSION_GENERATION_KEY = "platform_admin_session_generation"


def _check_csrf(request):
    def get_response(_request):
        return None

    check = CsrfViewMiddleware(get_response)
    check.process_request(request)
    reason = check.process_view(request, get_response, (), {})
    if reason:
        raise PermissionDenied(f"CSRF verification failed: {reason}")


class PlatformAdminSessionAuthentication(BaseAuthentication):
    def authenticate(self, request):
        session_key = request.COOKIES.get(
            settings.PLATFORM_ADMIN_SESSION_COOKIE_NAME
        )
        if not session_key:
            return None

        session = SessionStore(session_key=session_key)
        account_id = session.get(ADMIN_SESSION_KEY)
        generation = session.get(ADMIN_SESSION_GENERATION_KEY)
        if not account_id or not generation:
            raise AuthenticationFailed("Административная сессия недействительна.")

        try:
            account = PlatformAdminAccount.objects.select_related("rbac_user").get(
                pk=account_id,
                is_active=True,
                session_generation=generation,
                rbac_user__is_active=True,
            )
        except PlatformAdminAccount.DoesNotExist as exc:
            raise AuthenticationFailed("Административная сессия недействительна.") from exc

        _check_csrf(request._request)
        return account.rbac_user, account

    def authenticate_header(self, request):
        return "AdminSession"


class PlatformAdminAPIView(APIView):
    authentication_classes = (PlatformAdminSessionAuthentication,)


class AdminCsrfView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        return Response({"csrfToken": get_token(request._request)})


class AdminSessionView(APIView):
    authentication_classes = (PlatformAdminSessionAuthentication,)
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        return Response(
            {
                "authenticated": True,
                "username": request.auth.username,
            }
        )


class AdminLoginView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "admin_login"

    @method_decorator(csrf_protect)
    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or not username.strip()
            or len(password) > 1024
        ):
            return Response(
                {"detail": "Неверное имя администратора или пароль."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        account = (
            PlatformAdminAccount.objects.select_related("rbac_user")
            .filter(username__iexact=username.strip())
            .first()
        )
        if account is None:
            make_password(password)
            password_matches = False
        else:
            password_matches = account.check_password(password)
        valid = bool(
            account
            and account.is_active
            and account.rbac_user.is_active
            and password_matches
        )
        if not valid:
            return Response(
                {"detail": "Неверное имя администратора или пароль."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        session = SessionStore()
        session[ADMIN_SESSION_KEY] = account.pk
        session[ADMIN_SESSION_GENERATION_KEY] = str(account.session_generation)
        session.set_expiry(settings.PLATFORM_ADMIN_SESSION_AGE)
        session.create()

        response = Response(
            {"authenticated": True, "username": account.username},
            status=status.HTTP_200_OK,
        )
        response.set_cookie(
            key=settings.PLATFORM_ADMIN_SESSION_COOKIE_NAME,
            value=session.session_key,
            max_age=settings.PLATFORM_ADMIN_SESSION_AGE,
            httponly=True,
            secure=settings.PLATFORM_ADMIN_SESSION_COOKIE_SECURE,
            samesite=settings.PLATFORM_ADMIN_SESSION_COOKIE_SAMESITE,
            path="/api/",
        )
        return response


class AdminLogoutView(APIView):
    authentication_classes = (PlatformAdminSessionAuthentication,)
    permission_classes = (IsAuthenticated,)

    @method_decorator(csrf_protect)
    def post(self, request):
        session_key = request.COOKIES.get(
            settings.PLATFORM_ADMIN_SESSION_COOKIE_NAME
        )
        if session_key:
            SessionStore(session_key=session_key).delete()
        response = Response(status=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(
            key=settings.PLATFORM_ADMIN_SESSION_COOKIE_NAME,
            path="/api/",
            samesite=settings.PLATFORM_ADMIN_SESSION_COOKIE_SAMESITE,
        )
        return response
