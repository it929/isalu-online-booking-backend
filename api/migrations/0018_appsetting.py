from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('api', '0017_booking_referral_content')]
    operations = [migrations.CreateModel(name='AppSetting', fields=[
        ('key', models.CharField(max_length=100, primary_key=True, serialize=False)),
        ('value', models.JSONField(blank=True, default=dict)),
        ('updated_at', models.DateTimeField(auto_now=True)),
    ], options={'ordering': ['key']})]
