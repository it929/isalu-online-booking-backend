"""
Module Access (administrators only).

Assign dashboard modules ("desks") to user roles, and fine-tune a single user
on top of their role: grant extra modules, or take away modules their role
gives. Everything else (server permission checks, the dashboard sidebar)
already reads the user's modules from views.build_staff_profile, which applies
these settings, so no other part of the system changes.

Rules kept as they were:
- Administrators always have every module (their role/overrides don't limit them).
- Some modules come with others automatically (HMO desk -> Declined HMO
  Approvals and Reverse HMO Approval; Finance -> the financial and HMO modules,
  view-only; the private-patient desks -> Payment Reconciliation). A module an
  administrator takes away from a user stays away.
"""
from django.contrib.auth.models import User
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ModuleAccessChange, Role, UserProfile

MODULES = [
    # (id, label, group, description)
    ("helpdesk", "Helpdesk Reception", "Core Operations", "Today's patients, arrival and check-in status, booking lookups."),
    ("walk_in", "Walk-in Registration", "Core Operations", "Register walk-in patients (Helpdesk staff have it automatically)."),
    ("hmo", "HMO Insurance Desk", "Core Operations", "Approve or decline HMO/company patients and check approved patients in."),
    ("hmo_declined", "Declined HMO Approvals", "Core Operations", "Declined HMO requests (comes with the HMO desk)."),
    ("hmo_reversal", "Reverse HMO Approval", "Core Operations", "Reverse a granted HMO/company approval (comes with the HMO desk)."),
    ("cashdesk", "Cashdesk & Billing", "Core Operations", "Take payments at the hospital and clear patients."),
    ("cleared_patients", "Cleared Patients", "Core Operations", "Patients who have paid or been approved."),
    ("booking_payments", "Booking Payments", "Core Operations", "Online (Paystack) booking payments."),
    ("payment_reconciliation", "Payment Reconciliation", "Core Operations", "Check payments with Paystack, refunds, daily balancing, private check-in."),
    ("finance", "Finance (all financial & HMO features)", "Core Operations", "Every financial and HMO module; the desks themselves are view-only."),
    ("all_patients", "All Patients Directory", "Patient Directories", "Every booking record."),
    ("hmo_enrollees", "HMO Enrollees Registry", "Patient Directories", "HMO patients and enrollee IDs."),
    ("companies", "Companies", "Patient Directories", "Companies whose staff are booked as company patients."),
    ("private_patients", "Private Self-Pay List", "Patient Directories", "Private patients."),
    ("checked_in_patients", "Checked-in Queue", "Patient Directories", "Patients checked in today."),
    ("clinic", "Clinics & Departments", "Clinical & Scheduling", "Clinics, fees and online consultation fees."),
    ("create_specialist_schedule", "Specialist Roster", "Clinical & Scheduling", "Doctors' clinic days, cancelling or moving a clinic."),
    ("telemedicine", "Online Clinic (Video)", "Clinical & Scheduling", "Run online video clinics."),
    ("analytics", "Executive Analytics", "Command & Control", "Reports and queue analytics."),
    ("monitor", "Waiting Room Monitor", "Command & Control", "The waiting-room screen (cleared patients only)."),
    ("users", "Staff & Roles Registry", "Command & Control", "Staff accounts and roles."),
    ("disabled_bookings", "Archive & Trash", "Command & Control", "Disabled bookings."),
]
MODULE_IDS = [m[0] for m in MODULES]


def _views():
    from . import views
    return views


def _actor(user):
    return (user.get_full_name() or user.first_name or user.username) if user and user.is_authenticated else ""


def _clean(desks):
    from .views import ALL_DESKS
    if not isinstance(desks, (list, tuple)):
        return None
    out = []
    for d in desks:
        d = str(d or "").strip()
        if d in ALL_DESKS and d not in out:
            out.append(d)
    return out


def _role_payload(role):
    return {
        "role_id": role.role_id, "name": role.name, "description": role.description,
        "primary_desk": role.primary_desk, "allowed_desks": list(role.allowed_desks or []),
        "is_system_role": role.is_system_role, "status": role.status,
        "is_admin_role": any(k in str(role.name or "").lower() for k in _views().ADMIN_ROLE_KEYWORDS),
        "user_count": role.user_profiles.count(),
    }


def _user_payload(user):
    v = _views()
    prof = getattr(user, "profile", None)
    p = v.build_staff_profile(user)
    effective = list(p["allowedDesks"])
    # Helpdesk staff can register walk-ins (unless the module was taken away from them).
    if "helpdesk" in effective and "walk_in" not in effective and "walk_in" not in (p.get("removedDesks") or []):
        effective.append("walk_in")
    return {
        "id": user.id, "username": user.username, "name": user.first_name or user.get_full_name() or user.username,
        "email": user.email, "is_active": user.is_active,
        "role_id": prof.role.role_id if prof and prof.role else "", "role_name": p["role"],
        "is_admin": p["isAdmin"],
        "extra_desks": list(getattr(prof, "extra_desks", None) or []) if prof else [],
        "removed_desks": list(getattr(prof, "removed_desks", None) or []) if prof else [],
        "effective_desks": effective, "read_only_desks": p["readOnlyDesks"], "home_desk": p["desk"],
    }


def _change_payload(c):
    return {"id": c.id, "target_type": c.target_type, "target_id": c.target_id, "target_name": c.target_name,
            "before": c.before, "after": c.after, "actor": c.actor, "created_at": c.created_at.isoformat()}


class _AdminOnly(APIView):
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not _views().is_admin_user(request.user):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only administrators can manage module access.")


class ModuleAccessView(_AdminOnly):
    """GET: modules, roles, users (with their effective modules) and recent changes."""

    def get(self, request):
        users = User.objects.filter(is_active=True).select_related("profile", "profile__role").order_by("first_name", "username")
        return Response({
            "modules": [{"id": i, "label": l, "group": g, "description": d} for i, l, g, d in MODULES],
            "roles": [_role_payload(r) for r in Role.objects.all().order_by("name")],
            "users": [_user_payload(u) for u in users],
            "changes": [_change_payload(c) for c in ModuleAccessChange.objects.all()[:50]],
        })


class ModuleAccessRoleView(_AdminOnly):
    """PUT {allowed_desks: [...], primary_desk?}: the modules of a role (every user with the role)."""

    def put(self, request, role_id):
        role = Role.objects.filter(role_id=role_id).first()
        if role is None:
            return Response({"error": "Role not found."}, status=status.HTTP_404_NOT_FOUND)
        desks = _clean(request.data.get("allowed_desks"))
        if desks is None:
            return Response({"error": "allowed_desks must be a list of modules."}, status=status.HTTP_400_BAD_REQUEST)
        primary = str(request.data.get("primary_desk") or role.primary_desk or "").strip()
        if desks and primary not in desks:
            primary = desks[0]
        if not desks:
            return Response({"error": "A role needs at least one module. Disable the role instead if it shouldn't be used."},
                            status=status.HTTP_400_BAD_REQUEST)
        before = {"allowed_desks": list(role.allowed_desks or []), "primary_desk": role.primary_desk}
        with transaction.atomic():
            role.allowed_desks = desks
            role.primary_desk = primary
            role.save(update_fields=["allowed_desks", "primary_desk"])
            ModuleAccessChange.objects.create(target_type=ModuleAccessChange.TARGET_ROLE, target_id=role.role_id, target_name=role.name,
                                              before=before, after={"allowed_desks": desks, "primary_desk": primary}, actor=_actor(request.user))
        return Response({"message": f"Modules saved for the {role.name} role.", "role": _role_payload(role)})


class ModuleAccessUserView(_AdminOnly):
    """PUT {extra_desks: [...], removed_desks: [...]}: one user's changes on top of their role."""

    def put(self, request, user_id):
        user = User.objects.filter(id=user_id).select_related("profile").first()
        if user is None:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        extra = _clean(request.data.get("extra_desks", []))
        removed = _clean(request.data.get("removed_desks", []))
        if extra is None or removed is None:
            return Response({"error": "extra_desks and removed_desks must be lists of modules."}, status=status.HTTP_400_BAD_REQUEST)
        both = set(extra) & set(removed)
        if both:
            return Response({"error": f"A module can't be both granted and removed: {', '.join(sorted(both))}."},
                            status=status.HTTP_400_BAD_REQUEST)
        prof, _ = UserProfile.objects.get_or_create(user=user)
        before = {"extra_desks": list(prof.extra_desks or []), "removed_desks": list(prof.removed_desks or [])}
        with transaction.atomic():
            prof.extra_desks = extra
            prof.removed_desks = removed
            prof.save(update_fields=["extra_desks", "removed_desks"])
            ModuleAccessChange.objects.create(target_type=ModuleAccessChange.TARGET_USER, target_id=str(user.id),
                                              target_name=user.first_name or user.username, before=before,
                                              after={"extra_desks": extra, "removed_desks": removed}, actor=_actor(request.user))
        user = User.objects.select_related("profile", "profile__role").get(id=user.id)
        payload = _user_payload(user)
        note = " (administrators always have every module)" if payload["is_admin"] else ""
        return Response({"message": f"Module access saved for {payload['name']}{note}.", "user": payload})
