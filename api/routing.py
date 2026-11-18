# api/routing.py
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"^ws/notifications/$", consumers.MedicalBookingConsumer.as_asgi()),
    re_path(r"^ws/telemedicine/(?P<kind>patient|doctor)/(?P<key>[A-Za-z0-9_-]{6,80})/$", consumers.TelemedicineConsumer.as_asgi()),
]