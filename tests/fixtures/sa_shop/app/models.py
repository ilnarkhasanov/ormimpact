from __future__ import annotations

from typing import List, Optional

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    orders: Mapped[List["Order"]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_total", "total_cents"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    total: Mapped[int] = mapped_column("total_cents", default=0)
    note: Mapped[Optional[str]]
    customer: Mapped[Customer] = relationship(back_populates="orders")
