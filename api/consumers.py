# In backend/api/consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer

class MedicalBookingConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        # Patient records travel over this socket (patient_name, ref_code,
        # date, doctor_id via broadcast_booking_update). The public booking
        # form needs no auth, but this feed is staff-only, so reject
        # unauthenticated sockets before joining any group.
        if not (self.user and self.user.is_authenticated):
            await self.close(code=4401)
            return


        # Monitor-only staff never receive an uncleared patient's details.
        from channels.db import database_sync_to_async
        from .views import is_monitor_only
        self.monitor_only = await database_sync_to_async(is_monitor_only)(self.user)

        # Join the hospital staff real-time feed
        await self.channel_layer.group_add("hospital_feed", self.channel_name)

        # Join individual user room if authenticated
        if self.user and self.user.is_authenticated:
            await self.channel_layer.group_add(f"user_{self.user.id}", self.channel_name)

        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("hospital_feed", self.channel_name)
        if hasattr(self, "user") and self.user and self.user.is_authenticated:
            await self.channel_layer.group_discard(f"user_{self.user.id}", self.channel_name)

    async def receive(self, text_data):
        payload = json.loads(text_data)
        if payload.get("action") == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))

    @staticmethod
    def _looks_cleared(p):
        status = str(p.get("status") or "").strip().lower()
        if p.get("is_active") is False or status in ("cancelled", "canceled", "disabled", "awaiting payment", "payment expired"):
            return False
        pay = str(p.get("payment_status") or "").strip().lower()
        hmo = str(p.get("hmo_status") or "").strip().lower()
        return hmo != "declined" and (pay in ("cleared", "paid", "settled") or hmo == "approved")

    async def booking_update(self, event):
        """Dispatches real-time broadcast payload to WebSocket client."""
        payload = event["payload"]
        if getattr(self, "monitor_only", False) and not self._looks_cleared(payload):
            # Just "something changed" so the screen refreshes; the record itself
            # comes from the (filtered) sync endpoint.
            payload = {"event_type": "BOOKING_UPDATE", "message": "Booking updated.",
                       "timestamp": payload.get("timestamp"), "redacted": True}
        await self.send(text_data=json.dumps({
            "type": "BOOKING_UPDATE",
            "data": payload
        }))