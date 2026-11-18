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

class TelemedicineConsumer(AsyncWebsocketConsumer):
    """
    Signalling for online (video) consultations. The video itself flows
    directly between the two browsers (WebRTC); this only passes the
    connection set-up messages, and only while the doctor has admitted the
    patient and the patient's time hasn't run out.

    ws/telemedicine/patient/<video_token>/   the patient (no login; the token is the secret)
    ws/telemedicine/doctor/<session_id>/     staff with the Online Clinic desk (JWT ?token=)
    """
    MAX_MESSAGE = 64 * 1024

    async def connect(self):
        from channels.db import database_sync_to_async
        from . import telemedicine as tm
        kw = self.scope["url_route"]["kwargs"]
        self.kind = kw.get("kind")
        key = kw.get("key") or ""
        self.groups_joined = []
        if self.kind == "patient":
            info = await database_sync_to_async(self._patient_info)(key)
            if info is None:
                await self.close(code=4404)
                return
            self.ref, self.session_id = info
            await self._join(tm.patient_group(self.ref))
            await self.accept()
            await database_sync_to_async(tm.set_presence)(self.ref, True)
            await self.channel_layer.group_send(tm.doctor_group(self.session_id), {
                "type": "tm.event", "payload": {"event": "presence", "ref_code": self.ref, "in_waiting_room": True}})
            state = await database_sync_to_async(self._patient_state)()
            await self.send(text_data=json.dumps({"event": "state", "state": state}))
        elif self.kind == "doctor":
            user = self.scope.get("user")
            ok = await database_sync_to_async(self._doctor_ok)(user, key)
            if not ok:
                await self.close(code=4403)
                return
            self.session_id = key
            await self._join(tm.doctor_group(key))
            await self.accept()
        else:
            await self.close(code=4400)

    async def _join(self, group):
        await self.channel_layer.group_add(group, self.channel_name)
        self.groups_joined.append(group)

    async def disconnect(self, close_code):
        from channels.db import database_sync_to_async
        from . import telemedicine as tm
        for g in getattr(self, "groups_joined", []):
            await self.channel_layer.group_discard(g, self.channel_name)
        if getattr(self, "kind", "") == "patient" and getattr(self, "ref", None):
            await database_sync_to_async(tm.set_presence)(self.ref, False)
            await self.channel_layer.group_send(tm.doctor_group(self.session_id), {
                "type": "tm.event", "payload": {"event": "presence", "ref_code": self.ref, "in_waiting_room": False}})

    async def receive(self, text_data=None, bytes_data=None):
        from channels.db import database_sync_to_async
        from . import telemedicine as tm
        if not text_data or len(text_data) > self.MAX_MESSAGE:
            return
        try:
            msg = json.loads(text_data)
        except ValueError:
            return
        mtype = msg.get("type")
        if mtype == "ping":
            if self.kind == "patient":
                await database_sync_to_async(tm.set_presence)(self.ref, True)
            await self.send(text_data=json.dumps({"event": "pong"}))
            return
        if mtype != "signal":
            return
        data = msg.get("data")
        if self.kind == "patient":
            if not await database_sync_to_async(self._allowed)(self.ref):
                await self.send(text_data=json.dumps({"event": "not_allowed"}))
                return
            await self.channel_layer.group_send(tm.doctor_group(self.session_id), {
                "type": "tm.event", "payload": {"event": "signal", "ref_code": self.ref, "data": data}})
        else:
            ref = str(msg.get("ref_code") or "")
            if not await database_sync_to_async(self._doctor_may_signal)(ref):
                await self.send(text_data=json.dumps({"event": "not_allowed", "ref_code": ref}))
                return
            await self.channel_layer.group_send(tm.patient_group(ref), {
                "type": "tm.event", "payload": {"event": "signal", "data": data}})

    async def tm_event(self, event):
        await self.send(text_data=json.dumps(event["payload"]))

    # ---- database checks ------------------------------------------------
    def _patient_info(self, token):
        from .models import Booking
        from . import telemedicine as tm
        if len(token or "") < 20:
            return None
        b = Booking.objects.filter(video_token=token, consultation_mode=tm.ONLINE).only("ref_code", "online_session_id").first()
        if b is None or not b.online_session_id:
            return None
        return b.ref_code, b.online_session_id

    def _patient_state(self):
        from .models import Booking
        from . import telemedicine as tm
        b = Booking.objects.select_related("online_session").get(ref_code=self.ref)
        return tm.join_state(b)

    def _allowed(self, ref):
        from .models import Booking
        from . import telemedicine as tm
        b = Booking.objects.filter(ref_code=ref).first()
        return bool(b) and tm.signalling_allowed(b)

    def _doctor_ok(self, user, session_id):
        from .models import OnlineClinicSession
        from .telemedicine_views import can_host
        return can_host(user) and OnlineClinicSession.objects.filter(session_id=session_id).exists()

    def _doctor_may_signal(self, ref):
        from .models import Booking
        from . import telemedicine as tm
        b = Booking.objects.filter(ref_code=ref, online_session_id=self.session_id).first()
        return bool(b) and tm.signalling_allowed(b)
