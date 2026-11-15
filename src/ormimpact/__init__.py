"""ormimpact: ORM-aware impact analysis for domain-model changes.

Given a change to the domain model of a Django or SQLAlchemy project
(remove, rename, retype, add or extract a field), ormimpact lists the code
locations that are likely affected, including those a static type checker
cannot see: field names in strings (query lookups, serializers, admin,
forms, factories), raw SQL, fixtures, templates and history-based
co-changes. It can also run mypy or pyright before and after the change and
merge their new errors into the same report.
"""

__version__ = "0.2.0"
