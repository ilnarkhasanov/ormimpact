from django.db import connection


def raw_totals():
    with connection.cursor() as cur:
        cur.execute("SELECT o.id, o.total FROM shop_order o WHERE o.status = %s", ["paid"])
        return cur.fetchall()


def export(order):
    return {"total": getattr(order, "total"), "addr": order.delivery_address}


def touch(order):
    order.status = "x"
    order.save(update_fields=["status", "total"])
