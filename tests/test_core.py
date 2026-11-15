"""Building blocks of the impact analysis (v0.1): change specifications, migration detection, model discovery
(registry) for Django and SQLAlchemy, and source edits that apply a change to the model file."""
import pytest

from ormimpact.core import Change, parse_change
from ormimpact.edit import apply_edits, plan_edits
from ormimpact.project import Project, is_migration
from ormimpact.registry import build_registry


def test_parse_change_variants():
    """Every supported spelling of a change spec parses: ``remove``, ``rename`` with or without ``->``, ``retype``
    with a type after ``:``, and ``extract`` of several fields into a new model (one Change per field)."""
    assert parse_change("remove Order.total") == [Change("remove", "Order", "total")]
    assert parse_change("rename Order.total -> amount")[0].new_name == "amount"
    assert parse_change("rename Order.total amount")[0].new_name == "amount"
    assert parse_change("retype Order.total : Mapped[Decimal]")[0].new_type == "Mapped[Decimal]"
    ex = parse_change("extract Order.delivery_address,delivery_city -> Address")
    assert [c.field for c in ex] == ["delivery_address", "delivery_city"]
    assert all(c.op == "extract" and c.target == "Address" for c in ex)


@pytest.mark.parametrize("bad", ["drop Order.total", "rename Order.total", "remove total", "remove Order.a,b"])
def test_parse_change_errors(bad):
    """Malformed specs are rejected with ValueError: an unknown verb, a rename without a new name, a field without
    a model, and several fields for an operation other than ``extract``."""
    with pytest.raises(ValueError):
        parse_change(bad)


def test_is_migration():
    """Django (any path under ``migrations/``) and Alembic (``versions/`` inside ``alembic/`` or ``migrations/``)
    migration files are recognised, ordinary modules are not. Migrations are excluded from impact findings and are the source of schema operations."""
    assert is_migration("shop/migrations/0001_initial.py")
    assert is_migration("alembic/versions/abc_add.py")
    assert not is_migration("shop/models.py")


def test_django_registry(django_shop):
    """Django model discovery: default table name ``<app>_<model>``, fields inherited from an abstract base,
    ``<fk>_id`` column of a ForeignKey, reverse relations by ``related_name`` and by the default ``<model>_set``."""
    reg = build_registry(Project(django_shop))
    order = reg.get("Order")
    assert order.orm == "django" and order.table == "shop_order"
    assert order.fields["created"].inherited_from == "TimeStamped"
    assert order.fields["customer"].column == "customer_id"
    customer = reg.get("Customer")
    assert customer.reverse["orders"].origin == ("Order", "customer")
    assert "orderline_set" in order.reverse
    assert reg.get("TimeStamped").abstract


def test_sqlalchemy_registry(sa_shop):
    """SQLAlchemy 2.0 model discovery: ``__tablename__``, an explicit column name that differs from the attribute
    (``total`` -> ``total_cents``), ForeignKey targets, relationships, nullability from ``Optional[...]``; the
    DeclarativeBase itself is not a model."""
    reg = build_registry(Project(sa_shop))
    order = reg.get("Order")
    assert order.orm == "sqlalchemy" and order.table == "orders"
    assert order.fields["total"].column == "total_cents"
    assert order.fields["customer_id"].fk == "customers.id"
    assert order.fields["customer"].target == "Customer"
    assert order.fields["note"].nullable is True
    assert reg.get("Base") is None


def test_normalize_inherited_field(django_shop):
    """A change to an inherited field is redirected to the abstract model that declares it, with a warning
    (removing ``Order.created`` really changes every model derived from ``TimeStamped``)."""
    reg = build_registry(Project(django_shop))
    c, warnings = reg.normalize(Change("remove", "Order", "created"))
    assert c.model == "TimeStamped" and warnings


def test_plan_and_apply_edits(sa_shop):
    """Applying rename/retype/remove to a SQLAlchemy model file: the rename keeps the explicit column name (so the
    schema is unchanged), and the edited file still compiles. The checker layer runs mypy/pyright on this output."""
    reg = build_registry(Project(sa_shop))
    src = (sa_shop / "app/models.py").read_text()
    edits, w = plan_edits(reg, [Change("rename", "Order", "total", new_name="amount"),
                                Change("retype", "Order", "note", new_type="int"),
                                Change("remove", "Customer", "name")], {"app/models.py": src})
    assert not w
    out = apply_edits(src, edits["app/models.py"])
    assert 'amount: Mapped[int] = mapped_column("total_cents", default=0)' in out
    assert "note: Mapped[int]" in out
    assert "name: Mapped[str]" not in out
    compile(out, "models.py", "exec")


def test_django_retype_and_add_edits(django_shop):
    """Applying retype/add to a Django model file: the retype keeps the field's arguments, the added field gets a
    default field class, and the edited file still compiles."""
    reg = build_registry(Project(django_shop))
    src = (django_shop / "shop/models.py").read_text()
    edits, _ = plan_edits(reg, [Change("retype", "Order", "total", new_type="DecimalField"),
                                Change("add", "Order", "discount")], {"shop/models.py": src})
    out = apply_edits(src, edits["shop/models.py"])
    assert "total = models.DecimalField(default=0)" in out
    assert "discount = models.IntegerField()" in out
    compile(out, "models.py", "exec")
