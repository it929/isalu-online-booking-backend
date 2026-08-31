from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('api', '0016_alter_department_status_alter_doctor_status_and_more')]
    operations = [
        migrations.AddField(model_name='booking', name='referral_doc_data', field=models.TextField(blank=True, default='')),
        migrations.AddField(model_name='booking', name='referral_doc_text', field=models.TextField(blank=True, default='')),
    ]
