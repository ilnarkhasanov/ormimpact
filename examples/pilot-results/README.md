# Pilot results (November 2026)

Output of `ormimpact eval` on two open-source projects. These are pilot runs to
validate the tool, not thesis results: the samples are small, and file-level
ground truth from commits is noisy (CTFd release commits touch 50+ files).

| File | Command |
|---|---|
| `healthchecks_all_history_no_checker.txt` | `ormimpact eval healthchecks --mine 1000 --max-scan 5000` |
| `healthchecks_mypy_strict_since_2023-08.txt` | 15 model-touching commits since mypy was introduced, `--checker mypy --checker-cmd "venv/bin/mypy --strict ... hc"` (django-stubs plugin, project dependencies installed) |
| `ctfd_all_history_no_checker.txt` | `ormimpact eval CTFd --mine 1000 --max-scan 4000` |

Template findings were reported at medium confidence in these runs; their
measured precision (about 7%) led to lowering them to low confidence.

## Version-skew check (November 2026)

`healthchecks_skew.txt` / `.json`: the 19 healthchecks commits (2019–2026)
that add migrations with schema operations. The oracle ran each version's
Django test suite (about 1,660–1,780 tests) against the other version's
schema on SQLite, each version in a venv built from its own
`requirements.txt` (Python 3.12, falling back to 3.11):

```
ormimpact skew-eval healthchecks --commit ... --oracle django-sqlite --django-settings hc.settings \
    --oracle-requirements requirements.txt --oracle-base-python /usr/bin/python3.12 \
    --oracle-base-python /usr/bin/python3.11 --oracle-skip-package psycopg2
```

Caveats: one project; the rules for NOT NULL alterations were refined after
seeing the oracle on these commits, so the numbers are a development result,
not a held-out evaluation; the oracle only sees code paths the tests cover
(the one false positive, `create table logs_record`, is such a case).
