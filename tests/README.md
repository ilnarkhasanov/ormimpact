# Tests

```bash
uv sync          # installs the dev group: pytest, Django, SQLAlchemy, sqlglot, PyYAML, mypy
uv run pytest    # 37 tests, about 30 s
```

Tests that need an optional tool are skipped when it is missing: the mypy and pyright tests in
`test_git_eval.py` (tool on `PATH`) and the oracle test in `test_skew.py` (Django importable). pyright is not in
the dev group; install it separately (e.g. `brew install pyright` or `npm i -g pyright`) or its test is skipped.
Run `uv run pytest -rs` to see which tests were skipped and why.

## Files

| File | Covers | Part of the tool |
|---|---|---|
| `conftest.py` | `django_shop` / `sa_shop` fixtures (fresh copies in `tmp_path`), `git()` and `init_repo()` helpers with an isolated git identity | shared |
| `test_core.py` | change-spec parsing, migration detection, model registry for Django and SQLAlchemy, inherited fields, source edits | v0.1 impact analysis |
| `test_detectors.py` | ORM/config/dynamic/SQL/data/template layers: which references a planned change must find, and which look-alikes it must not | v0.1 impact analysis |
| `test_git_eval.py` | model diffs between revisions, commit mining and evaluation, mypy/pyright layer, `analyze`/`diff`/`eval` CLI | v0.1 impact analysis |
| `test_skew.py` | migration parsing (Django, Alembic), per-pair verdicts, linter baseline, `skew` CLI, the real oracle | v0.2 version-skew check (thesis) |

## Fixture projects

`fixtures/django_shop` and `fixtures/sa_shop` are small, non-runnable projects that contain one example of each
reference kind a type checker cannot see. Tests copy them, so they may be edited freely inside a test; never edit
the originals to make a test pass, since assertions refer to their exact lines.

* **django_shop** — `Customer`, `Order` (inherits `created` from the abstract `TimeStamped`), `OrderLine`;
  references to `Order` fields as strings and keywords in `views.py`, `admin.py`, `forms.py`, DRF `serializers.py`
  (including `source=` and `fields = "__all__"`), factories, a JSON fixture, `reports/monthly.sql` and a template.
  `cart.py` has an unrelated `Cart.total()` method that must *not* be reported.
* **sa_shop** — SQLAlchemy 2.0 `Customer`/`Order`; `Order.total` is mapped to the column `total_cents`, so raw SQL
  must be matched by column, not attribute. `app/legacy.py` is untyped (mypy's blind spot), `app/repo.py` is typed.

## Version-skew tests (`test_skew.py`)

These do not use the fixture projects. Each test writes two trees with `write()`: `before` (version N) is built by
`django_base()`, a minimal *runnable* Django project, and `after` (version N+1) adds new migrations. Assertions are
per pair:

* `PAIR_A` — old code on the new schema;
* `PAIR_B` — new code on the old schema.

| Test | Release | Pair A | Pair B | Linter |
|---|---|---|---|---|
| `test_drop_column_breaks_both_orders` | drop a still-mapped NOT NULL column | breaks | breaks | DROP_COLUMN |
| `test_add_nullable_column_needs_migrate_first` | add a nullable column | safe | breaks | not flagged (miss) |
| `test_state_only_removal_then_drop_is_safe` | drop a column version N no longer maps | safe; breaks if N has raw SQL on it | — | flagged (false positive) |
| `test_add_not_null_breaks_old_inserts` | add NOT NULL with Python-side `default=` | breaks | — | NOT_NULL |
| `test_rename_with_db_column_has_no_schema_op` | `RenameField` with `db_column` | no schema op | no schema op | — |
| `test_alembic_release` | Alembic drop + nullable add (incl. `batch_alter_table`) | breaks (drop) | breaks (add) | — |
| `test_set_not_null_breaks_when_old_code_leaves_field_unset` | alter to NOT NULL | breaks: high (prod) / medium (tests only) | — | — |
| `test_oracle_on_tiny_django_project` | the drop and add releases above, run for real on SQLite | matches static | matches static | — |

`test_set_not_null_…` encodes the NOT NULL alteration rule that was refined on the healthchecks pilot. Changing it
changes the pilot numbers, so follow the held-out protocol in `CLAUDE.md`.

## Adding a test

* Impact analysis: use `django_shop`/`sa_shop`; if a new reference kind is needed, add it to the fixture and check
  that line numbers asserted elsewhere still hold.
* Version skew: start from `django_base()` for both trees, add a migration to `after`, assert the verdict for both
  pairs and the linter baseline. If the scenario is something the oracle can confirm, add it to the oracle test.
