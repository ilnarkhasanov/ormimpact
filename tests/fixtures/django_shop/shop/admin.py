from django.contrib import admin

from .models import Customer, Order


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "total", "status")
    search_fields = ("delivery_address", "customer__name")
    list_filter = ["status"]


class CustomerAdmin(admin.ModelAdmin):
    search_fields = ["name", "email", "orders__delivery_address"]


admin.site.register(Customer, CustomerAdmin)
