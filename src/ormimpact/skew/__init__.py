"""Cross-version compatibility of model changes during deployment (version skew).

While a change is deployed, one database schema serves two code versions:

* pair A — old code N on the new schema N+1 (migrate first, rolling deploy, rollback);
* pair B — new code N+1 on the old schema N (deploy code first, migrate after).

This package derives the database operations of a release from its new
migration files (Django, Alembic), compares them with what each code version's
models map and how its code uses them, and reports which deployment orders
break.
"""
