from django.contrib import admin

from .models import (
    AIPlan,
    AIOperation,
    AIPipeline,
    PipelineVersion,
    TokenLedgerEntry,
    TokenWallet,
    UserEntitlement,
)


@admin.register(AIPlan)
class AIPlanAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "daily_operation_limit", "is_active")
    list_filter = ("is_active",)


@admin.register(UserEntitlement)
class UserEntitlementAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "updated_at")
    list_select_related = ("user", "plan")


@admin.register(TokenWallet)
class TokenWalletAdmin(admin.ModelAdmin):
    list_display = ("user", "balance", "updated_at")
    list_select_related = ("user",)


admin.site.register(TokenLedgerEntry)
admin.site.register(AIPipeline)
admin.site.register(AIOperation)
admin.site.register(PipelineVersion)
