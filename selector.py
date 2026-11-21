#!/usr/bin/env python3
"""Blast Radius Selector: Optimized test impact analyzer and worker scheduler."""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import sys
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MAP_PATH = Path(__file__).parent / "coverage_map.json.gz"


def duration_milliseconds(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)


def parse_patch(patch_text: str) -> dict[str, tuple[list[tuple[int, int, int, int]], list[str]]]:
    files = {}
    current_file = None
    hunks = []
    diff_lines = []

    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            if current_file:
                files[current_file] = (hunks, diff_lines)
            current_file = line[6:].strip()
            hunks = []
            diff_lines = []
        elif line.startswith("@@ ") and current_file:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                old_start = int(m.group(1))
                old_len = int(m.group(2)) if m.group(2) is not None else 1
                new_start = int(m.group(3))
                new_len = int(m.group(4)) if m.group(4) is not None else 1
                hunks.append((old_start, old_len, new_start, new_len))
        elif current_file and (line.startswith("+") or line.startswith("-")):
            diff_lines.append(line)

    if current_file:
        files[current_file] = (hunks, diff_lines)
    return files


def is_pure_comment_or_whitespace(files: dict) -> bool:
    if not files:
        return True

    for fn, (hunks, dlines) in files.items():
        if not fn.endswith(".py"):
            return False

        for dl in dlines:
            if dl.startswith("+") and not dl.startswith("+++"):
                stripped = dl[1:].strip()
                if stripped and not stripped.startswith("#"):
                    return False
            elif dl.startswith("-") and not dl.startswith("---"):
                stripped = dl[1:].strip()
                if stripped and not stripped.startswith("#"):
                    return False

    return True


def select_tests_for_patch(patch_text: str, cov: dict, tests_meta: list[dict]) -> set[int]:
    files = parse_patch(patch_text)
    if is_pure_comment_or_whitespace(files):
        return set()

    line_to_tests = cov.get("line_to_tests", {})
    func_to_tests = cov.get("func_to_tests", {})
    fixture_to_tests = cov.get("fixture_to_tests", {})
    file_to_tests = cov.get("file_to_tests", {})
    opt_test_map = cov.get("opt_test_map", {})
    fix_to_opt_test = cov.get("fix_to_opt_test", {})
    dialect_to_opt_tests = cov.get("dialect_to_opt_tests", {})
    executor_key_tests = cov.get("executor_key_tests", [])

    selected = set()

    for fn, (hunks, dlines) in files.items():
        if fn.startswith("tests/fixtures/"):
            rel_fix = fn.replace("tests/fixtures/", "")
            selected.update(fixture_to_tests.get(rel_fix, ()))
            fix_base = os.path.splitext(os.path.basename(fn))[0]
            if fix_base in fix_to_opt_test and fix_to_opt_test[fix_base] is not None:
                selected.add(fix_to_opt_test[fix_base])

        elif fn.startswith("tests/"):
            selected.update(file_to_tests.get(fn, ()))

        else:
            file_lines = line_to_tests.get(fn, {})
            file_funcs = func_to_tests.get(fn, [])

            for old_start, old_len, new_start, new_len in hunks:
                # Direct lines hit around hunk
                for l in range(old_start - 2, old_start + max(old_len, 1) + 3):
                    selected.update(file_lines.get(str(l), ()))

                # Smallest enclosing function
                for f_info in file_funcs:
                    if not (old_start + max(old_len, 1) < f_info["start"] or old_start > f_info["end"]):
                        selected.update(f_info["tests"])
                        break

            # Structural dependency mappings
            if fn.startswith("sqlglot/optimizer/"):
                base = os.path.splitext(os.path.basename(fn))[0]
                if base in fix_to_opt_test and fix_to_opt_test[base] is not None:
                    selected.add(fix_to_opt_test[base])

                for mname in ("test_optimize", "test_tpch", "test_tpcds"):
                    if mname in opt_test_map:
                        selected.add(opt_test_map[mname])

                if base in ("eliminate_joins", "eliminate_ctes", "pushdown_predicates"):
                    selected.update(executor_key_tests)

            elif "dialects/" in fn or "parsers/" in fn or "generators/" in fn or "typing/" in fn:
                dname = os.path.splitext(os.path.basename(fn))[0]
                dtest_file = f"tests/dialects/test_{dname}.py"
                selected.update(file_to_tests.get(dtest_file, ()))
                if dname in dialect_to_opt_tests:
                    selected.update(dialect_to_opt_tests[dname])

            elif fn == "sqlglot/serde.py":
                selected.update(file_to_tests.get("tests/test_serde.py", ()))
                selected.update(file_to_tests.get("tests/test_optimizer.py", ()))
                selected.update(file_to_tests.get("tests/test_expressions.py", ()))
                selected.update(executor_key_tests)

            elif fn in ("sqlglot/parser.py", "sqlglot/parsers/base.py"):
                selected.update(file_to_tests.get("tests/test_parser.py", ()))

            elif fn == "sqlglot/generator.py":
                selected.update(file_to_tests.get("tests/test_generator.py", ()))

            elif fn in ("sqlglot/tokens.py", "sqlglot/tokenizer_core.py"):
                selected.update(file_to_tests.get("tests/test_tokens.py", ()))

            elif fn == "sqlglot/transforms.py":
                selected.update(file_to_tests.get("tests/test_transforms.py", ()))

    return selected


def split_lpt(test_nums: list[int], k: int, durations: dict[int, int]) -> tuple[list[list[int]], int]:
    workers: list[list[int]] = [[] for _ in range(k)]
    loads = [0] * k

    for num in sorted(test_nums, key=lambda n: durations[n], reverse=True):
        lightest = loads.index(min(loads))
        workers[lightest].append(num)
        loads[lightest] += durations[num]

    occupied = [w for w in workers if w]
    max_load = max([sum(durations[t] for t in w) for w in occupied]) if occupied else 0
    return occupied, max_load


def optimize_workers(test_nums: list[int], durations: dict[int, int]) -> list[list[int]]:
    if not test_nums:
        return []

    best_cost = float("inf")
    best_workers: list[list[int]] = [test_nums]

    max_k = min(len(test_nums), 8)
    for k in range(1, max_k + 1):
        workers, max_l = split_lpt(test_nums, k, durations)
        cost = max_l + 2000 * len(workers)
        if cost < best_cost:
            best_cost = cost
            best_workers = workers

    return best_workers


def main() -> None:
    # 1. Load coverage map
    if MAP_PATH.exists():
        with gzip.open(MAP_PATH, "rt", encoding="utf-8") as f:
            cov = json.load(f)
    else:
        cov = {}

    # 2. Load tests metadata
    tests_file = DATA_DIR / "tests.json"
    tests_data = json.loads(tests_file.read_text(encoding="utf-8"))["tests"]
    durations = {t["number"]: duration_milliseconds(t["seconds"]) for t in tests_data}

    # 3. Process each commit patch
    commits_dir = DATA_DIR / "commits"
    patch_files = sorted(commits_dir.glob("*.patch"))

    selected_count = 0
    for patch_path in patch_files:
        cid = patch_path.stem
        patch_text = patch_path.read_text(encoding="utf-8")

        test_nums = select_tests_for_patch(patch_text, cov, tests_data)
        workers = optimize_workers(sorted(list(test_nums)), durations)

        selected_count += len(test_nums)
        line = json.dumps({"commit": cid, "workers": workers}, separators=(",", ":"))
        sys.stdout.write(line + "\n")

    sys.stdout.flush()
    print(f"Selector processed {len(patch_files)} commits (total {selected_count} tests selected)", file=sys.stderr)


if __name__ == "__main__":
    main()
