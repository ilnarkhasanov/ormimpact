# ormimpact

ORM-aware impact analysis for domain-model changes in Python projects that use
**Django ORM** or **SQLAlchemy** (including Flask-SQLAlchemy and SQLModel).

You describe a change to a model — remove, rename, retype or add a field, or
extract fields into a new entity — and ormimpact lists the code locations that
are likely affected. It combines the new errors of a static type checker
(mypy or pyright, run before and after the change) with analyses of the places
a type checker cannot see:

* field names inside strings: query lookups (`filter(customer__name=...)`,
  `order_by("-total")`, `F("total")`, `Q(...)`), serializers, forms, admin,
  django-filter, factories, marshmallow schemas, `Meta` options;
* dynamic access: `getattr(obj, "total")`, constructor keyword arguments
  (SQLAlchemy's default `__init__(**kwargs)` hides them from the checker),
  `save(update_fields=[...])`, attribute use inside unannotated functions;
* raw SQL in Python strings and `.sql` files (resolved with sqlglot:
  tables, aliases, columns);
* fixtures and data files, templates;
* co-change history mined from git.

Every finding carries a layer, a kind, a confidence and the evidence, so the
report can be evaluated layer by layer.

The tool is the artefact of a master's thesis on how Python static type
checkers behave under domain-model changes. It ships an evaluation mode that
replays real model-changing commits and measures precision and recall of each
layer against the files the developers actually changed.

## Version-skew check (`ormimpact skew`)

During a deployment, one database schema serves two code versions for a while:

| Pair | When it happens |
|---|---|
| **old code on the new schema** | migrate first, rolling deploy, rollback of the code after the migration |
| **new code on the old schema** | deploy the code first, migrate after (post-deploy migration) |

`ormimpact skew` reads the release's new migration files (Django operations,
including `SeparateDatabaseAndState`, and Alembic `op.*` / `batch_alter_table`),
derives the database operations, and checks each against what the *other*
code version maps and how its code uses it:

* a dropped or renamed column breaks the old version if its model still maps
  the column (the ORM selects every mapped column), or if its raw SQL names it;
* a NOT NULL column without a database default breaks the old version's inserts
  (Django's `default=` and SQLAlchemy's `default=` are Python-side), and the
  creation sites are listed;
* a new column, table or renamed column breaks the new version until the
  migration has run;
* dropping a NOT NULL column without a database default breaks the new
  version's inserts until the migration has run;
* changes of nullability and type are reported as risky.

```bash
ormimpact skew .                                   # working tree vs HEAD
ormimpact skew . --base v1.4 --head v1.5 -v
ormimpact skew --before-dir old/ --after-dir new/ -f md
ormimpact skew . --base origin/main --fail-on no-safe-order    # CI gate
```

The report gives a verdict per pair, per deployment order and per operation,
with evidence (the mapping line, the queries, the creation sites), the
operation-only verdict a migration linter would give, a recommended order and
a fix (for example a two-release split with `SeparateDatabaseAndState`).

### Test-based oracle (`ormimpact skew-eval`)

To measure the check, `skew-eval` runs the old version's test suite against a
database migrated by the new version (and the reverse), with migrations
disabled inside the test runner, and subtracts the failures each suite has on
its own schema. Remaining failures are real incompatibilities.

```bash
ormimpact skew-eval repo --mine 50                       # static verdicts and the linter baseline
ormimpact skew-eval repo --mine 50 --oracle django-sqlite \
    --django-settings myproject.settings --oracle-python venv/bin/python -o skew.json
ormimpact skew-eval repo --commit abc123 --oracle custom \
    --prepare-cmd "alembic upgrade head" --test-cmd "pytest -x"     # {db} is the database path
```

The built-in recipe targets Django projects that run on SQLite; `custom`
accepts any prepare/test commands (`{db}`, `{tree}`, `{python}` are expanded,
`ORMIMPACT_DB` is set). The aggregate reports precision and recall of the
static check and of the linter against the oracle, per pair.

## Install

```bash
pip install -e ".[all]"        # sqlglot, PyYAML, mypy
# or minimal: pip install -e .  (pure standard library; SQL falls back to token matching)
```

Python 3.10+. The analysed project does not have to be importable: models are
found statically with `ast`. The checker layer runs the project's own mypy or
pyright, so for that layer the project's dependencies should be installed.

## Quick start

```bash
ormimpact models path/to/project                 # what models and fields were found

ormimpact analyze path/to/project -c "rename Order.total -> amount"
ormimpact analyze . -c "remove Order.delivery_address" --checker mypy -v
ormimpact analyze . -c "extract Order.delivery_address,delivery_city -> Address" -f md -o impact.md

ormimpact diff .                                 # model changes in the working tree vs HEAD
ormimpact diff . --base origin/main --fail-on high   # for CI

ormimpact mine path/to/repo                      # commits that touch model declarations
ormimpact eval path/to/repo --mine 100 -o results.json
```

### Change specifications

| Specification | Meaning |
|---|---|
| `remove Order.total` | attribute removed |
| `rename Order.total -> amount` | attribute renamed |
| `retype Order.total : Mapped[Decimal]` | type change (SQLAlchemy annotation) |
| `retype Order.total : DecimalField` | type change (Django field class or full expression) |
| `add Order.discount : Mapped[int]` | new attribute |
| `extract Order.delivery_address,delivery_city -> Address` | Extract Entity: the fields move to a new model |

A change on an inherited field is moved to the class that defines it, so all
subclasses are covered. The new type of `retype` and `add` is only needed for
the checker run.

## Layers

| Layer | Thesis layer | What it reports |
|---|---|---|
| `checker` | L1 | new mypy/pyright errors after applying the change to a copy of the project |
| `orm` | L2 | Django lookups in kwargs and strings, `F`/`Q`/aggregates, related managers; SQLAlchemy `filter_by`, `values`, loader options, `relationship(back_populates=...)`, `ForeignKey("table.col")`, `__table_args__`, `__table__.c.x`, bulk mappings |
| `config` | L2 | DRF serializers (`fields`, `source=`, `__all__`), forms, admin, generic views, django-filter, factory_boy, marshmallow, model `Meta` (ordering, indexes, constraints) |
| `dynamic` | L2 | `getattr`/`setattr`/`hasattr`, constructor kwargs, `update_fields`, `self.x` inside model methods, attribute use on variables named after the model |
| `sql` | L3 | raw SQL in code (`execute`, `text`, `raw`, `RunSQL`, SQL-looking strings) and `.sql` files |
| `data` | L3 | Django fixtures, JSON/YAML/CSV files keyed by the column |
| `template` | L3 | `{{ order.total }}`, `{% ... %}` in Django/Jinja templates |
| `history` | L4 | files that changed together with the model's file (support, confidence) |
| `text` | baseline | every occurrence of the identifier (grep); off by default, on in `eval` |

Confidence:

* **high** — the reference is resolved to the changed attribute of the changed
  model (for example the lookup root is `Order.objects`, or the serializer's
  `Meta.model = Order`);
* **medium** — the model is inferred (related manager, variable named `order`,
  unique field name), or the change is a `retype`, where the name still works;
* **low** — matched by name only.

For `add`, the tool reports creation sites, factories, fixtures and `INSERT`
statements that do not set a new required field, and explicit field lists that
do not expose it.

## Type checker integration

```bash
ormimpact analyze . -c "remove Order.total" --checker mypy
ormimpact analyze . -c "remove Order.total" --checker pyright
ormimpact analyze . -c "remove Order.total" --checker mypy --checker-args "--strict" --checker-targets "src"
ormimpact analyze . -c "remove Order.total" --checker-cmd "venv/bin/mypy --strict --show-error-codes --no-pretty --show-column-numbers --hide-error-context src"
```

The checker runs on the unchanged tree and on a temporary copy with the change
applied; only errors that are new after the change are reported (line numbers
are compared only in files that were not edited). `--checker auto` (the
default for `analyze`) uses mypy or pyright when the project has a
configuration for it.

## Evaluation

`ormimpact eval REPO` follows the protocol of Qiu et al. (FSE 2013):

1. For a commit that changes model declarations, the parent revision is the
   *before* tree; the parent plus only the commit's model files is the *after*
   tree.
2. The model changes are derived by diffing the two model registries
   (renames and Extract Entity are recognised).
3. **Ground truth**: files modified in the same commit, excluding the model
   files, migrations, newly added files, documentation and packaging files.
4. Each configuration (L1, L2, L3, L4, text baseline and their unions)
   predicts a set of files; precision, recall and F1 are computed per commit
   and aggregated (micro and macro).
5. Results are also broken down by change category (`breaking` = remove,
   rename or extract; `retype`; `add-only`) and by detector kind, which gives
   file-level precision for each kind of finding.

Co-change history is mined only from commits before the evaluated one.

```bash
ormimpact eval repo --mine 200 --category breaking -o results.json
ormimpact eval repo --commits-file shas.txt --checker mypy \
    --checker-cmd "venv/bin/mypy --strict --no-error-summary --show-error-codes --no-color-output --hide-error-context --show-column-numbers --no-pretty src"
```

Known threats to validity: commits can be tangled (unrelated edits inflate the
ground truth), fixes can land in later commits, file-level granularity is
coarse, and for the checker layer the result depends on the installed
dependencies and checker version.

## Output

`--format text` (default), `json` or `md`; `--min-confidence low|medium|high`;
`-v` adds the change, the enclosing function and the evidence detail;
`--fail-on high` makes the command exit with status 1 (for CI).

## Limitations and roadmap

* Resolution is static and convention-based: a queryset stored in a variable
  loses its model, so its lookups are matched by name with lower confidence.
* Django multi-table inheritance, custom lookups and `GenericForeignKey` are
  handled approximately.
* Function-level ground truth and paired statistics (Wilcoxon, Cliff's delta)
  are planned on top of the per-commit JSON output.
* An LLM-based detector as an additional baseline.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The test projects in `tests/fixtures` (a Django shop and an SQLAlchemy shop)
contain one example of each blind spot.

## License

MIT
