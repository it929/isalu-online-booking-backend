from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0027_booking_updated_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='booking',
            name='hmo_decline_reason',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='booking',
            name='hmo_declined_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='booking',
            name='hmo_declined_by',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
    ]
