# ormimpact — context for Claude Code

This repository is the tool for Ilnar's master's thesis. Read this file first; it replaces the chat history in
which the project was designed.

## Thesis topic (fixed)

**Cross-Version Schema Compatibility in ORM-Based Python Applications: An Empirical Study and Static Detection of
Deployment-Unsafe Model Changes.**

During a deployment one database schema serves two code versions:

* **pair A — old code on the new schema** (migrate first, rolling deploy, rollback);
* **pair B — new code on the old schema** (deploy code first, post-deploy migration).

The thesis studies how often model changes in Django/SQLAlchemy projects break one of the pairs, how many of those
breakages migration linters miss, and builds a static, code-aware checker evaluated against a test-based oracle.

Research questions:

* RQ1 — prevalence of cross-version incompatibilities in model-changing commits;
* RQ2 — share that depends on application code and is missed or mislabelled by migration linters;
* RQ3 — precision/recall of the static checker against the oracle, compared with linters and type checkers;
* RQ4 (optional) — incomplete contract phase (dead columns) and its lifetime.

Novelty, scoped (do not overclaim): no prior work found statically analyses application code to decide whether a
model change is compatible with the *adjacent* code version under a deployment order. Not novel: the mixed-version
problem itself (Dumitraș & Narasimhan 2009; Neamtiu & Dumitraș 2011; de Jong & van Deursen 2015/2017), online
schema change (F1, QuantumDB, InVerDa, BullFrog), migration linters (django-migration-linter, strong_migrations,
Atlas), same-version schema–code checks (SUITE, Meurice et al. 2016, EvolutionSaver ASE 2021, orm-lens), upgrade
checks for distributed systems (DUPChecker SOSP 2021, UpFuzz NSDI 2026). The closest research group:
Shan Lu / Junwen Yang / Yongle Zhang — re-check their publications regularly.
The full literature review lives in a Claude Doc: https://claude.ai/artifact/8ZBxTxFfrhfSPwatpR82yd

## Layout

```
src/ormimpact/
  cli.py, __main__.py               argparse entry point (`ormimpact`, `python -m ormimpact`)
  core.py, project.py, astutil.py   data types, file discovery, AST helpers
  gitutil.py                        git subprocess helpers (export a revision's tree, changed files, log)
  registry.py                       model discovery (Django, SQLAlchemy, Flask-SQLAlchemy, SQLModel) incl. DB props
  detectors/                        impact layers: base.py (interface), django_orm.py, sqlalchemy_orm.py, config.py,
                                    dynamic.py, sql.py, data.py, templates.py, history.py, text.py
  checker.py, edit.py               mypy/pyright before/after, applying a change spec
  engine.py, diff.py, report.py     `analyze` / `diff` pipeline (impact analysis, v0.1)
  evaluate.py                       commit-based evaluation of impact analysis (v0.1)
  skew/                             THE THESIS PART (v0.2)
    schema.py    mapping (what a version's models map) + parsing new Django/Alembic migrations into DbOps
    rules.py     verdicts per pair (breaks/risky/safe), evidence, fixes, linter baseline
    engine.py    analyze_trees / analyze_git, text/json/md output
    oracle.py    test-based oracle: old tests on new schema and vice versa (django-sqlite recipe, custom cmds,
                 per-version cached venvs from requirements.txt)
    evaluate.py  skew-eval over commits, confusion matrices vs oracle
tests/          pytest; tests/test_skew.py includes a real oracle run on a tiny Django project
examples/       pilot results and merge_skew_results.py (recompute static verdicts without rerunning tests)
```

Commands: `ormimpact skew`, `ormimpact skew-eval`, plus the older `analyze`, `diff`, `models`, `mine`, `eval`.

## Key design decisions

* Database operations come from the release's **new migration files**, not from model diffs: model state and
  database schema can differ (`SeparateDatabaseAndState`, `db_column`). Model diffs without migrations are notes.
* ORM semantics matter more than references: a column still mapped by version N's model breaks every query on that
  model in pair A, even with no reference to the field in code.
* Django `default=` and SQLAlchemy `default=` are Python-side; only `db_default` / `server_default` count as database
  defaults. A Django string field has an implicit `""` default only when `null=False`.
* NOT NULL additions/alterations break pair A only through creation sites that leave the field unset; sites are
  split into production and tests (tests-only → medium confidence).
* Oracle: test runs must not migrate the prepared schema (the recipe patches Django's `migrate` command to a no-op
  via `ORMIMPACT_SKIP_MIGRATE`); failures on the own schema are subtracted; Django's parallel runner needs `tblib`.

## Pilot results (healthchecks, 19 schema-changing commits, 2019–2026)

| Pair | Oracle breaks | Static P / R (`breaks`) | Static P / R (`breaks` or `risky`) | Linter P / R |
|---|---|---|---|---|
| old code on new schema | 7 | 100% / 100% | 64% / 100% | 64% / 100% |
| new code on old schema | 11 | 92% / 100% | 92% / 100% | 46% / 46% |

Report both static rows: in pair A all 4 `risky` verdicts (nullable → NOT NULL alterations) were compatible per the
oracle, so counting `risky` as a positive drops precision to the linter's level.

Caveats: one project; the NOT NULL alteration rule was refined after seeing the oracle on these commits (development
result, not held-out); the oracle only covers tested code paths (the one FP, `create table logs_record`).
Files: `examples/pilot-results/healthchecks_skew.{txt,json}`; also v0.1 impact-analysis runs on healthchecks and
CTFd (`*_all_history_no_checker.txt`, `healthchecks_mypy_strict_since_2023-08.txt`).

Reproduce (oracle recipe `django-sqlite`, each version in a venv from its own `requirements.txt`):

```
ormimpact skew-eval healthchecks --commit ... --oracle django-sqlite --django-settings hc.settings \
    --oracle-requirements requirements.txt --oracle-base-python <python3.12> \
    --oracle-base-python <python3.11> --oracle-skip-package psycopg2
```

The pilot ran on Linux (system packages `libcurl4-openssl-dev`, `libpq-dev`). On macOS use `brew install curl libpq`;
python3.12 and python3.11 come from pyenv (`~/.pyenv/shims/python3.12`, `python3.11`).

## Held-out evaluation protocol

* The rules are frozen at git tag `rules-frozen-pilot` (state after the healthchecks pilot). Every held-out run must
  record the commit/tag of the rules it used.
* Held-out projects (fill in as they are chosen; never tune rules on them before their first run): —
* Any rule change after a held-out run creates a new tag and makes that project a development set from then on.

## Next steps

1. Find 3–5 more open-source Django/SQLAlchemy projects whose tests run on SQLite (or add a PostgreSQL recipe) and
   run `skew-eval` **without changing the rules** — the held-out evaluation for RQ3.
2. Measure RQ1/RQ2 at scale (static only) over many repositories; record linter disagreements by category.
3. Add an Alembic/pytest oracle recipe; consider a PostgreSQL recipe via Docker.
4. Optional RQ4: dead columns (mapped by no model, no raw SQL) and their lifetime.
5. Before submission: re-run the literature search protocol (see the review), verify quotes against PDFs.

## Conventions

* Setup: `uv sync --extra dev` (pytest, Django, SQLAlchemy, sqlglot, PyYAML, mypy into `.venv`).
* Tests: `uv run pytest` (37 tests, about 30 s; the oracle test needs Django, included in `dev`). Keep all tests
  green.
* The repository is under git; commit before and after rule changes so evaluation runs can cite a revision.
* Never change rules and evaluate on the same commits without saying so; keep a held-out set.
* User's language: Russian in conversation; code, docs and thesis text in English.
