from ormimpact.core import Change, parse_change
from ormimpact.engine import analyze


def _hits(report, min_conf="low"):
    from ormimpact.core import at_least
    return {(f.path, f.line, f.kind) for f in report.findings if at_least(f.confidence, min_conf)}


def _files(report, min_conf="low"):
    return {p for p, _, _ in _hits(report, min_conf)}


def test_django_rename_covers_string_references(django_shop):
    r = analyze(django_shop, parse_change("rename Order.total -> amount"), checker=None)
    kinds = {k for _, _, k in _hits(r)}
    for expected in ["filter-kwarg", "order_by-string", "Sum-string", "update-kwarg", "F-string",
                     "list_display", "fields", "read_only_fields", "factory-attribute", "Meta.indexes",
                     "self-attribute", "getattr-literal", "save-update_fields", "django-fixture",
                     "sql-file", "sql-string", "template-attribute", "values_list-string"]:
        assert expected in kinds, expected
    files = _files(r)
    assert "shop/cart.py" not in files            # Cart.total is a method of another class
    assert not any("migrations" in f for f in files)


def test_django_precision_on_other_models(django_shop):
    r = analyze(django_shop, parse_change("remove Customer.email"), checker=None)
    lines = _hits(r)
    assert ("shop/views.py", 27, "order_by-string") in lines
    assert ("shop/admin.py", 14, "search_fields") in lines
    assert not any(p == "shop/serializers.py" for p, _, _ in lines)


def test_django_reverse_relation_via_fk_change(django_shop):
    r = analyze(django_shop, parse_change("rename Order.customer -> buyer"), checker=None)
    kinds = {(p, k) for p, _, k in _hits(r)}
    assert ("shop/views.py", "Sum-string") in kinds           # orders__total through the reverse name
    assert ("shop/admin.py", "search_fields") in kinds        # customer__name and orders__delivery_address
    assert ("shop/views.py", "create-kwarg") in kinds


def test_django_source_kwarg_and_all_fields(django_shop):
    r = analyze(django_shop, parse_change("remove Order.delivery_address"), checker=None)
    hits = _hits(r)
    assert ("shop/serializers.py", 7, "declared-source") in hits
    assert ("shop/serializers.py", 18, "all-fields") in hits
    assert ("shop/forms.py", 9, "fields") in hits
    assert ("shop/views.py", 11, "Q-kwarg") in hits


def test_django_add_required_field(django_shop):
    r = analyze(django_shop, [Change("add", "Order", "currency", required=True)], checker=None)
    kinds = {k for _, _, k in _hits(r)}
    assert "create-missing-new-field" in kinds
    assert "factory-missing-new-field" in kinds
    assert "fixture-missing-new-field" in kinds
    assert "fields-list-add" in kinds


def test_sqlalchemy_remove(sa_shop):
    r = analyze(sa_shop, parse_change("remove Order.total"), checker=None)
    kinds = {(p, k) for p, _, k in _hits(r)}
    for expected in [("app/repo.py", "filter_by-kwarg"), ("app/repo.py", "constructor-kwarg"),
                     ("app/repo.py", "values-kwarg"), ("app/repo.py", "bulk_insert_mappings"),
                     ("app/repo.py", "sql-string"), ("sql/report.sql", "sql-file"),
                     ("app/models.py", "Index-column"), ("app/legacy.py", "attribute-by-variable-name")]:
        assert expected in kinds, expected


def test_sqlalchemy_relationship_back_populates(sa_shop):
    r = analyze(sa_shop, parse_change("rename Customer.orders -> purchases"), checker=None)
    assert ("app/models.py", "relationship-back_populates") in {(p, k) for p, _, k in _hits(r)}


def test_sql_unrelated_table_not_reported(sa_shop):
    (sa_shop / "sql" / "other.sql").write_text("SELECT total_cents FROM invoices;\n")
    r = analyze(sa_shop, parse_change("remove Order.total"), checker=None)
    assert "sql/other.sql" not in _files(r)


def test_retype_downgrades_confidence(django_shop):
    r = analyze(django_shop, parse_change("retype Order.total : DecimalField"), checker=None)
    assert r.findings
    assert all(f.confidence != "high" for f in r.findings if f.layer in ("orm", "config"))
