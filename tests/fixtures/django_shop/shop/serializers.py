from rest_framework import serializers

from .models import Order


class OrderSerializer(serializers.ModelSerializer):
    address = serializers.CharField(source="delivery_address")

    class Meta:
        model = Order
        fields = ["id", "customer", "total", "status", "address"]
        read_only_fields = ("total",)


class FullOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = "__all__"
