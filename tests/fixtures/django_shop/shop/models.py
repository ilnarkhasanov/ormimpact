from django.db import models


class TimeStamped(models.Model):
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class Customer(TimeStamped):
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)


class Order(TimeStamped):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="orders")
    delivery_address = models.CharField(max_length=255)
    total = models.IntegerField(default=0)
    status = models.CharField(max_length=20)

    class Meta:
        ordering = ["-created"]
        indexes = [models.Index(fields=["status", "total"])]

    def is_big(self):
        return self.total > 1000


class OrderLine(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE)
    product = models.CharField(max_length=100)
    quantity = models.IntegerField()
