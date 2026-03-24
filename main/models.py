from django.db import models
from django.contrib.auth.models import AbstractUser
from django.contrib import admin
import uuid
from django.utils.text import slugify


class User(AbstractUser):
    user_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    user_name = models.CharField(
        max_length=20,
        unique=True,
        verbose_name='Имя пользователя',
        db_index=True
    )

    slug = models.SlugField(max_length=100, unique=True, blank=True)

    email = models.EmailField(
        verbose_name='Электронная почта',
        max_length=254,
        unique=True
    )


    USERNAME_FIELD = 'user_name'  
    REQUIRED_FIELDS = ['email']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.user_name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.user_name
    
    
class Projects(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    work_type = models.CharField(max_length=10)
    date = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=15)
    data = models.JSONField()