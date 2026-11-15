from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from .models import Customer, Order


def big_orders(session: Session) -> list[Order]:
    return list(session.scalars(select(Order).where(Order.total > 1000)))


def total_of(order: Order) -> int:
    return order.total


def zero_orders(session: Session) -> list[Order]:
    return session.query(Order).filter_by(total=0).all()


def raw(session: Session):
    return session.execute(text("SELECT id, total_cents FROM orders WHERE total_cents > :x"), {"x": 1})


def create(session: Session, c: Customer) -> Order:
    o = Order(customer=c, total=5)
    session.add(o)
    return o


def dyn(o):
    return getattr(o, "total")


def bulk(session: Session) -> None:
    session.execute(update(Order).values(total=0))
    session.bulk_insert_mappings(Order, [{"total": 1, "customer_id": 1}])


def names(session: Session) -> list[str]:
    return list(session.scalars(select(Customer.name)))
