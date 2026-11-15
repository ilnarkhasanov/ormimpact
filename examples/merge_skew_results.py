"""Merge oracle results from several skew-eval runs and recompute the static verdicts.

Usage: python merge_skew_results.py REPO OUT.json RUN1.json [RUN2.json ...]

The oracle labels are taken from the runs (later runs win); the static verdicts
and the linter baseline are recomputed with the current rules, so a change of
the rules does not require running the test suites again.
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from ormimpact.skew.evaluate import CommitSkew, aggregate, evaluate_commit, format_aggregate, format_rows


def main() -> None:
    repo, out, runs = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
    labels: dict[str, dict] = {}
    order: list[str] = []
    for run in runs:
        for c in json.loads(Path(run).read_text())["commits"]:
            if c["sha"] not in order:
                order.append(c["sha"])
            if c.get("oracle_a") in ("breaks", "compatible") or c["sha"] not in labels:
                labels[c["sha"]] = c
    rows: list[CommitSkew] = []
    for sha in order:
        r = evaluate_commit(repo, sha, None)
        lab = labels[sha]
        r.oracle_a, r.oracle_b = lab.get("oracle_a", ""), lab.get("oracle_b", "")
        r.oracle_fail_a, r.oracle_fail_b = lab.get("oracle_fail_a", 0), lab.get("oracle_fail_b", 0)
        if r.oracle_a == "error":
            r.reason = lab.get("reason", "")
        rows.append(r)
    agg = aggregate(rows)
    Path(out).write_text(json.dumps({"aggregate": agg, "commits": [asdict(r) for r in rows]}, indent=2))
    print(format_rows(rows))
    print()
    print(format_aggregate(agg))


if __name__ == "__main__":
    main()
