# In backend/api/consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer

class MedicalBookingConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")

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

    async def booking_update(self, event):
        """Dispatches real-time broadcast payload to WebSocket client."""
        await self.send(text_data=json.dumps({
            "type": "BOOKING_UPDATE",
            "data": event["payload"]
        }))