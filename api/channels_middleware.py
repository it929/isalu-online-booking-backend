import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import AccessToken


User = get_user_model()
logger = logging.getLogger(__name__)


# @database_sync_to_async
# def get_user_from_token(token_string):
#     """
#     Validate a SimpleJWT access token and return the authenticated user.

#     Returns AnonymousUser() when the token is missing, invalid, expired,
#     or the referenced user does not exist/is inactive.
#     """
#     try:
#         validated_token = AccessToken(token_string)

#         # Respect the USER_ID_CLAIM configured in SIMPLE_JWT.
#         simple_jwt_settings = getattr(settings, "SIMPLE_JWT", {})
#         user_id_claim = simple_jwt_settings.get("USER_ID_CLAIM", "user_id")

#         user_id = validated_token.get(user_id_claim)

#         if not user_id:
#             return AnonymousUser()

#         return User.objects.get(
#             pk=user_id,
#             is_active=True,
#         )

#     except (
#         InvalidToken,
#         TokenError,
#         User.DoesNotExist,
#         KeyError,
#         TypeError,
#         ValueError,
#     ):
#         return AnonymousUser()

@database_sync_to_async
def get_user_from_token(token_string):
    """
    Validate a SimpleJWT access token and return the user, or AnonymousUser.
    Logs at DEBUG level only: token payloads and staff emails must not be
    written to production logs on every WebSocket connection.
    """
    if not token_string:
        logger.debug("WebSocket auth: no token supplied")
        return AnonymousUser()
    try:
        user_id = AccessToken(token_string).payload.get("user_id")
    except (TokenError, InvalidToken) as exc:
        logger.debug("WebSocket auth: invalid token (%s)", type(exc).__name__)
        return AnonymousUser()
    except Exception:
        logger.exception("WebSocket auth: unexpected token error")
        return AnonymousUser()
    if not user_id:
        return AnonymousUser()
    user = User.objects.filter(id=user_id, is_active=True).first()
    if user is None:
        logger.debug("WebSocket auth: user %s missing or inactive", user_id)
        return AnonymousUser()
    return user


class JWTAuthMiddleware:
    """
    Channels middleware that authenticates WebSocket connections
    using a SimpleJWT access token supplied through:

        /ws/notifications/?token=<JWT>

    The authenticated user is placed in:

        scope["user"]
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        # Make a copy so the original scope is not modified unexpectedly.
        scope = dict(scope)

        query_string = scope.get("query_string", b"")

        try:
            query_string = query_string.decode("utf-8")
        except UnicodeDecodeError:
            query_string = ""

        query_params = parse_qs(query_string)

        # Frontend sends:
        # ?token=<encoded JWT>
        token = query_params.get("token", [None])[0]

        if token:
            scope["user"] = await get_user_from_token(token)
        else:
            scope["user"] = AnonymousUser()

        return await self.inner(scope, receive, send)


def JWTAuthMiddlewareStack(inner):
    """
    Preserve the existing API used by clinic_backend/asgi.py.
    """
    return JWTAuthMiddleware(inner)