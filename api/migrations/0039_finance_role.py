from django.db import migrations

DESCRIPTION = ("All financial and HMO/company features: booking payments, payment reconciliation, refunds, "
               "the money ledger, cashdesk, HMO approvals and companies.")


def add_finance_role(apps, schema_editor):
    Role = apps.get_model("api", "Role")
    if Role.objects.filter(role_id="role-7").exists() or Role.objects.filter(name__iexact="Finance Officer").exists():
        return
    Role.objects.create(role_id="role-7", name="Finance Officer", description=DESCRIPTION,
                        primary_desk="payment_reconciliation", allowed_desks=["finance"],
                        is_system_role=True, status=True)


def remove_finance_role(apps, schema_editor):
    Role = apps.get_model("api", "Role")
    Role.objects.filter(role_id="role-7", name="Finance Officer").delete()


class Migration(migrations.Migration):
    dependencies = [("api", "0038_booking_arrival")]
    operations = [migrations.RunPython(add_finance_role, remove_finance_role)]
