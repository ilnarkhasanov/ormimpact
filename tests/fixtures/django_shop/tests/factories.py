import factory

from shop.models import Order


class OrderFactory(factory.django.DjangoModelFactory):
    total = 100
    status = "new"
    customer = factory.SubFactory("tests.factories.CustomerFactory")

    class Meta:
        model = Order
