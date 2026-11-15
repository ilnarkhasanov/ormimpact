from django.db.models import F, Q, Sum

from .models import Customer, Order


def big_orders():
    return Order.objects.filter(total__gte=1000).order_by("-total")


def by_address(addr: str):
    return Order.objects.filter(Q(delivery_address__icontains=addr) | Q(status="new"))


def customer_totals():
    return Customer.objects.annotate(spent=Sum("orders__total")).values("name", "spent")


def related(customer):
    return customer.orders.filter(status="paid").values_list("total", flat=True)


def bump(order_id):
    Order.objects.filter(pk=order_id).update(total=F("total") + 1)


def unrelated():
    return Customer.objects.filter(name="x").order_by("email")


def make(customer):
    return Order.objects.create(customer=customer, delivery_address="a", status="new")


def lines(order):
    return order.orderline_set.all()
