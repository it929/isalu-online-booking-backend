"""
clinic_backend URL Configuration
"""

from django.contrib import admin
from django.urls import path, include
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

import re

from django.conf import settings

from api.superuser_tools import tools_page

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api-auth/', include('rest_framework.urls', namespace='rest_framework')),
    
    # JWT Authentication Endpoints
    path('api/auth/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    
    # Core API Endpoints
    path('api/', include('api.urls')),
]

# Hidden superuser bookings tools: only when SUPERUSER_TOOLS_PATH is set to a
# long secret slug (letters, digits, - and _; 24+ characters). Served under
# /api/ so it works through the same proxy as the API. Listed before the API
# routes so the slug can't be taken by them.
_tools_slug = str(getattr(settings, "SUPERUSER_TOOLS_PATH", "") or "").strip().strip("/")
if re.fullmatch(r"[A-Za-z0-9_-]{24,128}", _tools_slug):
    urlpatterns.insert(0, path(f"api/{_tools_slug}/", tools_page, name="superuser_tools"))
