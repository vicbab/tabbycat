# Generated by Django 2.2.4 on 2019-09-06 17:10

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('options', '0007_auto_20181224_1719'),
    ]

    operations = [
        migrations.AlterField(
            model_name='tournamentpreferencemodel',
            name='section',
            field=models.CharField(blank=True, db_index=True, default=None, max_length=150, null=True, verbose_name='Section Name'),
        ),
    ]
