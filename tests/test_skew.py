import json
import sys
import textwrap
from pathlib import Path

import pytest

from ormimpact.cli import main
from ormimpact.skew.engine import analyze_trees
from ormimpact.skew.rules import PAIR_A, PAIR_B, linter_verdict

try:
    import django  # noqa: F401
    HAVE_DJANGO = True
except ImportError:
    HAVE_DJANGO = False


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip())


DJ_SETTINGS = """
import os
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET_KEY = "x"
INSTALLED_APPS = ["django.contrib.contenttypes", "shop"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.path.join(BASE_DIR, "db.sqlite3")}}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
USE_TZ = True
"""
DJ_MANAGE = """
import os, sys
if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
"""
DJ_0001 = """
from django.db import migrations, models

class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Order",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("note", models.CharField(max_length=50, null=True)),
                ("total", models.IntegerField(default=0)),
            ],
        ),
    ]
"""
DJ_TESTS_BEFORE = """
from django.test import TestCase
from .models import Order

class OrderTests(TestCase):
    def test_create_and_read(self):
        Order.objects.create(note="a", total=5)
        self.assertEqual(Order.objects.get(note="a").total, 5)

    def test_count(self):
        Order.objects.create(note="b")
        self.assertEqual(Order.objects.count(), 1)
"""


def django_base(root: Path) -> None:
    write(root, {
        "manage.py": DJ_MANAGE, "proj/__init__.py": "", "proj/settings.py": DJ_SETTINGS,
        "shop/__init__.py": "", "shop/migrations/__init__.py": "", "shop/migrations/0001_initial.py": DJ_0001,
        "shop/models.py": """
            from django.db import models

            class Order(models.Model):
                note = models.CharField(max_length=50, null=True)
                total = models.IntegerField(default=0)
        """,
        "shop/views.py": """
            from .models import Order

            def make(note):
                return Order.objects.create(note=note)

            def big():
                return Order.objects.filter(total__gte=100)
        """,
        "shop/tests.py": DJ_TESTS_BEFORE,
    })


@pytest.fixture
def drop_release(tmp_path):
    before, after = tmp_path / "drop" / "before", tmp_path / "drop" / "after"
    django_base(before)
    django_base(after)
    write(after, {
        "shop/models.py": """
            from django.db import models

            class Order(models.Model):
                note = models.CharField(max_length=50, null=True)
        """,
        "shop/views.py": """
            from .models import Order

            def make(note):
                return Order.objects.create(note=note)
        """,
        "shop/tests.py": """
            from django.test import TestCase
            from .models import Order

            class OrderTests(TestCase):
                def test_create(self):
                    Order.objects.create(note="a")
                    self.assertEqual(Order.objects.count(), 1)
        """,
        "shop/migrations/0002_remove_order_total.py": """
            from django.db import migrations

            class Migration(migrations.Migration):
                dependencies = [("shop", "0001_initial")]
                operations = [migrations.RemoveField(model_name="order", name="total")]
        """,
    })
    return before, after


@pytest.fixture
def add_nullable_release(tmp_path):
    before, after = tmp_path / "add" / "before", tmp_path / "add" / "after"
    django_base(before)
    django_base(after)
    write(after, {
        "shop/models.py": """
            from django.db import models

            class Order(models.Model):
                note = models.CharField(max_length=50, null=True)
                total = models.IntegerField(default=0)
                coupon = models.CharField(max_length=20, null=True)
        """,
        "shop/migrations/0002_order_coupon.py": """
            from django.db import migrations, models

            class Migration(migrations.Migration):
                dependencies = [("shop", "0001_initial")]
                operations = [migrations.AddField(model_name="order", name="coupon",
                                                  field=models.CharField(max_length=20, null=True))]
        """,
    })
    return before, after


def _v(rep, pair, kind):
    return next(v for v in rep.verdicts if v.pair == pair and v.op.kind == kind)


def test_drop_column_breaks_both_orders(drop_release):
    rep = analyze_trees(*drop_release)
    assert [o.describe() for o in rep.ops] == ["drop column shop_order.total"]
    a, b = _v(rep, PAIR_A, "drop_column"), _v(rep, PAIR_B, "drop_column")
    assert a.status == "breaks" and a.confidence == "high"
    assert any("views.py" in e.path for e in a.evidence)          # the old code's lookup total__gte
    assert b.status == "breaks"                                 # NOT NULL column, new code inserts without it
    assert rep.summary["recommendation"].startswith("no safe order")
    assert linter_verdict(rep.ops[0]) == (True, "DROP_COLUMN")


def test_add_nullable_column_needs_migrate_first(add_nullable_release):
    rep = analyze_trees(*add_nullable_release)
    assert _v(rep, PAIR_A, "add_column").status == "safe"
    assert _v(rep, PAIR_B, "add_column").status == "breaks"
    assert rep.summary["recommendation"].startswith("migrate first")
    assert linter_verdict(rep.ops[0]) == (False, "")


def test_state_only_removal_then_drop_is_safe(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    for root in (before, after):
        django_base(root)
        write(root, {"shop/models.py": """
            from django.db import models

            class Order(models.Model):
                note = models.CharField(max_length=50, null=True)
        """, "shop/views.py": "from .models import Order\n"})
    write(after, {"shop/migrations/0003_drop_total.py": """
        from django.db import migrations

        class Migration(migrations.Migration):
            dependencies = [("shop", "0001_initial")]
            operations = [migrations.SeparateDatabaseAndState(
                database_operations=[migrations.RemoveField(model_name="order", name="total")])]
    """})
    rep = analyze_trees(before, after)
    assert _v(rep, PAIR_A, "drop_column").status == "safe"      # the running version no longer maps it
    assert linter_verdict(rep.ops[0])[0] is True                 # the linter still flags it
    # raw SQL in the old version that names the column makes it break again
    write(before, {"shop/report.py": """
        from django.db import connection

        def totals():
            with connection.cursor() as c:
                c.execute("SELECT sum(total) FROM shop_order")
    """})
    rep = analyze_trees(before, after)
    assert _v(rep, PAIR_A, "drop_column").status == "breaks"


def test_add_not_null_breaks_old_inserts(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    django_base(before)
    django_base(after)
    write(after, {
        "shop/models.py": """
            from django.db import models

            class Order(models.Model):
                note = models.CharField(max_length=50, null=True)
                total = models.IntegerField(default=0)
                paid = models.BooleanField(default=False)
        """,
        "shop/migrations/0002_order_paid.py": """
            from django.db import migrations, models

            class Migration(migrations.Migration):
                dependencies = [("shop", "0001_initial")]
                operations = [migrations.AddField(model_name="order", name="paid",
                                                  field=models.BooleanField(default=False))]
        """,
    })
    rep = analyze_trees(before, after)
    a = _v(rep, PAIR_A, "add_column")
    assert a.status == "breaks" and any("views.py" in e.path for e in a.evidence)
    assert linter_verdict(rep.ops[0]) == (True, "NOT_NULL")


def test_rename_with_db_column_has_no_schema_op(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    django_base(before)
    django_base(after)
    write(after, {
        "shop/models.py": """
            from django.db import models

            class Order(models.Model):
                note = models.CharField(max_length=50, null=True)
                amount = models.IntegerField(default=0, db_column="total")
        """,
        "shop/migrations/0002_rename.py": """
            from django.db import migrations

            class Migration(migrations.Migration):
                dependencies = [("shop", "0001_initial")]
                operations = [migrations.RenameField(model_name="order", old_name="total", new_name="amount")]
        """,
    })
    rep = analyze_trees(before, after)
    assert rep.ops == []


def test_alembic_release(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    models_before = """
        from typing import Optional
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

        class Base(DeclarativeBase):
            pass

        class Order(Base):
            __tablename__ = "orders"
            id: Mapped[int] = mapped_column(primary_key=True)
            total: Mapped[int] = mapped_column("total_cents", default=0)
    """
    models_after = """
        from typing import Optional
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

        class Base(DeclarativeBase):
            pass

        class Order(Base):
            __tablename__ = "orders"
            id: Mapped[int] = mapped_column(primary_key=True)
            note: Mapped[Optional[str]]
    """
    write(before, {"app/__init__.py": "", "app/models.py": models_before,
                   "app/repo.py": "from .models import Order\n\ndef mk(s):\n    s.add(Order(total=1))\n",
                   "alembic/versions/001_init.py": "revision = '001'\ndef upgrade():\n    pass\n"})
    write(after, {"app/__init__.py": "", "app/models.py": models_after,
                  "app/repo.py": "from .models import Order\n\ndef mk(s):\n    s.add(Order(note='x'))\n",
                  "alembic/versions/001_init.py": "revision = '001'\ndef upgrade():\n    pass\n",
                  "alembic/versions/002_change.py": """
                      import sqlalchemy as sa
                      from alembic import op

                      revision = "002"
                      down_revision = "001"

                      def upgrade():
                          op.add_column("orders", sa.Column("note", sa.String(), nullable=True))
                          with op.batch_alter_table("orders") as batch_op:
                              batch_op.drop_column("total_cents")
                  """})
    rep = analyze_trees(before, after)
    kinds = sorted(o.describe() for o in rep.ops)
    assert kinds == ["add column orders.note (NULL)", "drop column orders.total_cents"]
    assert _v(rep, PAIR_A, "drop_column").status == "breaks"
    assert _v(rep, PAIR_B, "add_column").status == "breaks"
    assert _v(rep, PAIR_A, "add_column").status == "safe"


def test_cli_skew_dirs_json(drop_release, capsys):
    before, after = drop_release
    rc = main(["skew", "--before-dir", str(before), "--after-dir", str(after), "-f", "json", "--fail-on", "any"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["summary"]["pair_a"] == "breaks"
    assert data["linter"][0]["rule"] == "DROP_COLUMN"


@pytest.mark.skipif(not HAVE_DJANGO, reason="Django not installed")
def test_oracle_on_tiny_django_project(drop_release, add_nullable_release):
    from ormimpact.skew.oracle import check, django_sqlite

    cfg = django_sqlite("proj.settings", python=sys.executable, parallel=None)
    res = check(*drop_release, cfg)
    assert res["A"].status == "breaks", res["A"].reason          # old code selects the dropped column
    assert res["B"].status == "breaks", res["B"].reason          # new code inserts without the NOT NULL column
    res = check(*add_nullable_release, cfg)
    assert res["A"].status == "compatible", res["A"].reason      # nullable column: old code keeps working
    assert res["B"].status == "breaks", res["B"].reason          # new code selects a column that is not there yet


def test_set_not_null_breaks_when_old_code_leaves_field_unset(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    for root in (before, after):
        django_base(root)
    models = """
        from django.db import models

        class Order(models.Model):
            note = models.CharField(max_length=50, null=True)
            total = models.IntegerField(default=0)
            customer = models.ForeignKey("Customer", null=True, on_delete=models.CASCADE)

        class Customer(models.Model):
            name = models.CharField(max_length=50)
    """
    write(before, {"shop/models.py": models,
                   "shop/views.py": "from .models import Order\n\ndef make(c):\n    return Order.objects.create(customer=c)\n"
                                    "\n\ndef make2(c):\n    return Order(customer_id=c.id)\n"})
    write(after, {"shop/models.py": models.replace("null=True)\n            total", "max_length=50, null=False)\n            total")
                                          .replace("CharField(max_length=50, max_length=50, null=False)", "CharField(max_length=50)"),
                  "shop/migrations/0002_note_not_null.py": """
                      from django.db import migrations, models

                      class Migration(migrations.Migration):
                          dependencies = [("shop", "0001_initial")]
                          operations = [
                              migrations.AlterField(model_name="order", name="note", field=models.CharField(max_length=50)),
                              migrations.AlterField(model_name="order", name="customer",
                                                    field=models.ForeignKey("Customer", on_delete=models.CASCADE)),
                          ]
                  """})
    rep = analyze_trees(before, after)
    by_col = {v.op.column: v for v in rep.verdicts if v.pair == PAIR_A and v.op.kind == "alter_column"}
    assert by_col["note"].status == "breaks"           # Order.objects.create(customer=c) leaves note NULL
    # production code always sets customer (or customer_id); only the old tests create orders without it
    cust = by_col["customer_id"]
    assert cust.status == "breaks" and cust.confidence == "medium"
    assert all("tests" in e.path for e in cust.evidence if e.kind != "model")
