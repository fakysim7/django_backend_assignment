from django.contrib import admin
from .models import User

@admin.register(User)
class UsersAdmin(admin.ModelAdmin):
    list_display = ['user_name', 'slug']
    prepopulated_fields = {'slug': ('slug',)}


# Register your models here.
