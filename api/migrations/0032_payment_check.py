from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0031_visit_type_and_follow_up_fee"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaymentCheck",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reference", models.CharField(db_index=True, max_length=100)),
                ("ref_code", models.CharField(blank=True, db_index=True, default="", max_length=20)),
                ("patient_name", models.CharField(blank=True, default="", max_length=200)),
                ("action", models.CharField(default="check", max_length=10)),
                ("paystack_status", models.CharField(blank=True, default="", max_length=30)),
                ("amount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("verdict", models.CharField(blank=True, default="", max_length=40)),
                ("note", models.CharField(blank=True, default="", max_length=300)),
                ("checked_by", models.CharField(blank=True, default="", max_length=200)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
