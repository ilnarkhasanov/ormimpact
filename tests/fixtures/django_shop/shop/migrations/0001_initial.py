from django.db import migrations, models

class Migration(migrations.Migration):
    operations = [migrations.AddField("order", "total", models.IntegerField(default=0))]
