from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import AccessToken


User = get_user_model()


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
    print("\n========== WEBSOCKET JWT DEBUG ==========")
    print("JWT received:", bool(token_string))
    print("JWT length:", len(token_string) if token_string else 0)

    if not token_string:
        print("❌ FAILURE: No JWT token received")
        print("========================================\n")
        return AnonymousUser()

    try:
        validated_token = AccessToken(token_string)

        print("✅ JWT signature/structure accepted")

        # Safely inspect the JWT payload
        payload = validated_token.payload

        print("JWT payload:", payload)

        user_id = payload.get("user_id")

        print("JWT user_id:", user_id)

        if not user_id:
            print("❌ FAILURE: JWT does not contain user_id")
            print("========================================\n")
            return AnonymousUser()

        try:
            user = User.objects.get(id=user_id)

            print("✅ Django user found")
            print("User ID:", user.id)
            print("Username:", getattr(user, "username", None))
            print("Email:", getattr(user, "email", None))
            print("is_active:", user.is_active)

            if not user.is_active:
                print("❌ FAILURE: Django user is inactive")
                print("========================================\n")
                return AnonymousUser()

            print("✅ JWT AUTHENTICATION SUCCESS")
            print("========================================\n")

            return user

        except User.DoesNotExist:
            print(
                "❌ FAILURE: No Django user exists for user_id:",
                user_id
            )
            print("========================================\n")
            return AnonymousUser()

    except TokenError as e:
        print("❌ FAILURE: JWT TokenError")
        print("Error:", repr(e))
        print("========================================\n")
        return AnonymousUser()

    except InvalidToken as e:
        print("❌ FAILURE: InvalidToken")
        print("Error:", repr(e))
        print("========================================\n")
        return AnonymousUser()

    except Exception as e:
        print("❌ FAILURE: Unexpected JWT error")
        print("Exception type:", type(e).__name__)
        print("Exception:", repr(e))
        print("========================================\n")
        return AnonymousUser()

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