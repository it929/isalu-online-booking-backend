"""
Django settings for clinic_backend project.
Generated for Isalu Hospitals Medical Booking Platform.
"""

from pathlib import Path
from datetime import timedelta
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-isalu-clinic-booking-key-2026-secure-key!')

DEBUG = os.getenv('DJANGO_DEBUG', 'True').lower() in ('true', '1')

ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '*').split(',')

# Application definition
INSTALLED_APPS = [
    "daphne",
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    "channels",
    
    # Third party packages
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    
    # Local apps
    'api.apps.ApiConfig',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'clinic_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'clinic_backend.wsgi.application'
ASGI_APPLICATION = 'clinic_backend.asgi.application'

# =========================================================
# REDIS & ASYNC CHANNEL LAYER CONFIGURATION (PRODUCTION READY)
# =========================================================
REDIS_URL = os.getenv('REDIS_URL', '').strip()
REDIS_HOST = os.getenv('REDIS_HOST', '').strip()
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', '').strip()

if REDIS_URL:
    redis_hosts = [REDIS_URL]
elif REDIS_HOST:
    if REDIS_PASSWORD:
        redis_hosts = [f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"]
    else:
        redis_hosts = [(REDIS_HOST, REDIS_PORT)]
else:
    redis_hosts = None

if redis_hosts:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": redis_hosts,
            },
        },
    }
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL or (f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0" if REDIS_PASSWORD else f"redis://{REDIS_HOST}:{REDIS_PORT}/0"),
        }
    }
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        },
    }
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }

# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# REST Framework Configuration & Security
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle'
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100000/day',
        'user': '500000/day'
    }
}

# JWT Token Configuration
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=7),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# CORS & CSRF Configuration for Mobile & Web Clients
CORS_ALLOW_ALL_ORIGINS = os.getenv('CORS_ALLOW_ALL_ORIGINS', 'True' if DEBUG else 'False').lower() in ('true', '1')
CORS_ALLOW_CREDENTIALS = True

cors_allowed_hosts = os.getenv('CORS_ALLOWED_ORIGINS', '').split(',')
if cors_allowed_hosts and cors_allowed_hosts[0]:
    CORS_ALLOWED_ORIGINS = [h.strip() for h in cors_allowed_hosts if h.strip()]

custom_csrf_origins = os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',')
default_csrf_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:8000",
]
if custom_csrf_origins and custom_csrf_origins[0]:
    CSRF_TRUSTED_ORIGINS = default_csrf_origins + [o.strip() for o in custom_csrf_origins if o.strip()]
else:
    CSRF_TRUSTED_ORIGINS = default_csrf_origins

# Production Security Headers
if not DEBUG:
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'True').lower() in ('true', '1')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Lagos'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# =========================================================
# =========================================================
# EMAIL SMTP CONFIGURATION (FOR PATIENT REMINDERS & NOTIFICATIONS)
# =========================================================
_email_user = os.getenv('EMAIL_HOST_USER', '').strip()
_email_backend_env = os.getenv('EMAIL_BACKEND', '').strip()

if _email_backend_env:
    EMAIL_BACKEND = _email_backend_env
elif _email_user and not _email_user.startswith("your-"):
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True').lower() in ('true', '1')
EMAIL_USE_SSL = os.getenv('EMAIL_USE_SSL', 'False').lower() in ('true', '1')
EMAIL_HOST_USER = _email_user
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', f"Isalu Hospitals <{_email_user or 'no-reply@isalu.ng'}>")

# =========================================================
# SMS GATEWAY CONFIGURATION (EBULKSMS.COM API)
# =========================================================
EBULKSMS_USERNAME = os.getenv('EBULKSMS_USERNAME', '').strip()
EBULKSMS_API_KEY = os.getenv('EBULKSMS_API_KEY', '').strip()
EBULKSMS_SENDER_ID = os.getenv('EBULKSMS_SENDER_ID', 'ISALU').strip()
EBULKSMS_API_URL = os.getenv('EBULKSMS_API_URL', 'https://api.ebulksms.com/sendsms.json').strip()

