#!/usr/bin/env python3
"""Blast Radius Selector X2: High-precision test impact analyzer & optimal worker scheduler."""

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
        # Non-code files (documentation, markdown, configs) never break tests
        if any(fn.endswith(ext) for ext in (".md", ".rst", ".txt", ".yml", ".yaml", ".json", ".lock")):
            continue
        if fn.startswith(".github/") or fn.startswith(".git") or fn in ("LICENSE", "NOTICE"):
            continue

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


def get_targeted_fallback(file_path: str, cov: dict, fast_file_tests: dict) -> set[int]:
    """Fast, sub-second targeted test suite for any file in sqlglot if line-coverage returns empty."""
    target = set()
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    file_to_tests = cov.get("file_to_tests", {})
    dialect_to_opt_tests = cov.get("dialect_to_opt_tests", {})
    fix_to_opt_test = cov.get("fix_to_opt_test", {})
    opt_test_map = cov.get("opt_test_map", {})

    if file_path.startswith("sqlglot/dialects/") or file_path.startswith("sqlglot/parsers/") or file_path.startswith("sqlglot/generators/"):
        dtest = f"tests/dialects/test_{base_name}.py"
        target.update(file_to_tests.get(dtest, ()))
        if base_name in dialect_to_opt_tests:
            target.update(dialect_to_opt_tests[base_name])

    elif file_path.startswith("sqlglot/optimizer/"):
        if base_name in fix_to_opt_test and fix_to_opt_test[base_name] is not None:
            target.add(fix_to_opt_test[base_name])
        for m in ("test_optimize", "test_tpch", "test_tpcds"):
            if m in opt_test_map:
                target.add(opt_test_map[m])

    elif file_path.startswith("sqlglot/expressions/"):
        target.update(file_to_tests.get("tests/test_expressions.py", ()))

    elif file_path in ("sqlglot/tokens.py", "sqlglot/tokenizer_core.py"):
        target.update(file_to_tests.get("tests/test_tokens.py", ()))

    elif file_path == "sqlglot/transforms.py":
        target.update(file_to_tests.get("tests/test_transforms.py", ()))

    elif file_path == "sqlglot/diff.py":
        target.update(file_to_tests.get("tests/test_diff.py", ()))

    elif file_path == "sqlglot/schema.py":
        target.update(file_to_tests.get("tests/test_schema.py", ()))

    elif file_path == "sqlglot/lineage.py":
        target.update(file_to_tests.get("tests/test_lineage.py", ()))

    elif file_path == "sqlglot/jsonpath.py":
        target.update(file_to_tests.get("tests/test_jsonpath.py", ()))

    elif file_path == "sqlglot/anonymize.py":
        target.update(file_to_tests.get("tests/test_anonymize.py", ()))

    elif file_path == "sqlglot/serde.py":
        target.update(file_to_tests.get("tests/test_serde.py", ()))
        for m in ("test_canonicalize", "test_optimize"):
            if m in opt_test_map:
                target.add(opt_test_map[m])

    elif file_path.startswith("sqlglot/executor/") or file_path == "sqlglot/planner.py":
        executor_key_tests = cov.get("executor_key_tests", [])
        target.update(executor_key_tests)
        target.update(fast_file_tests.get("tests/test_executor.py", ())[:10])

    elif file_path == "sqlglot/time.py":
        target.update(file_to_tests.get("tests/test_time.py", ()))

    elif file_path == "sqlglot/errors.py":
        target.update(file_to_tests.get("tests/test_errors.py", ()))

    elif file_path in ("sqlglot/parser.py", "sqlglot/parsers/base.py"):
        # Select fastest parser tests (<1.0s), never the full 309s file!
        target.update(fast_file_tests.get("tests/test_parser.py", ()))

    elif file_path == "sqlglot/generator.py":
        target.update(fast_file_tests.get("tests/test_generator.py", ()))

    else:
        for tf, tnums in file_to_tests.items():
            if base_name in tf:
                target.update(tnums[:5])
                break

    return target


def select_tests_for_patch(patch_text: str, cov: dict, fast_file_tests: dict) -> set[int]:
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
        file_selected = set()

        if fn.startswith("tests/fixtures/"):
            rel_fix = fn.replace("tests/fixtures/", "")
            file_selected.update(fixture_to_tests.get(rel_fix, ()))
            fix_base = os.path.splitext(os.path.basename(fn))[0]
            if fix_base in fix_to_opt_test and fix_to_opt_test[fix_base] is not None:
                file_selected.add(fix_to_opt_test[fix_base])

        elif fn.startswith("tests/"):
            file_selected.update(file_to_tests.get(fn, ()))

        else:
            file_lines = line_to_tests.get(fn, {})
            file_funcs = func_to_tests.get(fn, [])

            has_func_match = False
            for old_start, old_len, new_start, new_len in hunks:
                # Direct lines hit around hunk
                for l in range(old_start - 2, old_start + max(old_len, 1) + 3):
                    file_selected.update(file_lines.get(str(l), ()))

                # Smallest enclosing function (sorted by span ascending)
                hunk_start = old_start
                hunk_end = old_start + (max(old_len, 1) - 1 if old_len > 0 else 1)
                for f_info in file_funcs:
                    if not (hunk_end < f_info["start"] or hunk_start > f_info["end"]):
                        file_selected.update(f_info["tests"])
                        has_func_match = True
                        break

            # Structural dependency mappings
            if fn.startswith("sqlglot/optimizer/"):
                base = os.path.splitext(os.path.basename(fn))[0]
                if base in fix_to_opt_test and fix_to_opt_test[base] is not None:
                    file_selected.add(fix_to_opt_test[base])

                for mname in ("test_canonicalize", "test_optimize", "test_tpch", "test_tpcds"):
                    if mname in opt_test_map:
                        file_selected.add(opt_test_map[mname])

                if base in ("eliminate_joins", "eliminate_ctes", "pushdown_predicates"):
                    file_selected.update(executor_key_tests)

            elif "dialects/" in fn or "parsers/" in fn or "generators/" in fn or "typing/" in fn:
                dname = os.path.splitext(os.path.basename(fn))[0]
                dtest_file = f"tests/dialects/test_{dname}.py"
                file_selected.update(file_to_tests.get(dtest_file, ()))
                # If modified lines didn't hit any inner function (e.g. class attribute like WEEK_OFFSET),
                # or if it's a dialect-level parser, include optimizer tests that exercise this dialect
                if not has_func_match or "parsers/" in fn:
                    if dname in dialect_to_opt_tests:
                        file_selected.update(dialect_to_opt_tests[dname])

            elif fn == "sqlglot/serde.py":
                file_selected.update(file_to_tests.get("tests/test_serde.py", ()))
                file_selected.update(file_to_tests.get("tests/test_optimizer.py", ()))
                file_selected.update(executor_key_tests)

            elif fn.startswith("sqlglot/expressions/"):
                file_selected.update(file_to_tests.get("tests/test_expressions.py", ()))

            elif fn in ("sqlglot/tokens.py", "sqlglot/tokenizer_core.py"):
                file_selected.update(file_to_tests.get("tests/test_tokens.py", ()))

            elif fn == "sqlglot/transforms.py":
                file_selected.update(file_to_tests.get("tests/test_transforms.py", ()))

            elif fn == "sqlglot/diff.py":
                file_selected.update(file_to_tests.get("tests/test_diff.py", ()))

            elif fn == "sqlglot/schema.py":
                file_selected.update(file_to_tests.get("tests/test_schema.py", ()))

            elif fn == "sqlglot/lineage.py":
                file_selected.update(file_to_tests.get("tests/test_lineage.py", ()))

            elif fn == "sqlglot/jsonpath.py":
                file_selected.update(file_to_tests.get("tests/test_jsonpath.py", ()))

            elif fn == "sqlglot/anonymize.py":
                file_selected.update(file_to_tests.get("tests/test_anonymize.py", ()))

        # Safety Net: if code was modified but file_selected returned empty, run fast targeted fallback!
        if not file_selected:
            file_selected = get_targeted_fallback(fn, cov, fast_file_tests)

        selected.update(file_selected)

    return selected


def split_lpt(test_nums: list[int], k: int, durations: dict[int, int]) -> tuple[list[list[int]], int]:
    workers: list[list[int]] = [[] for _ in range(k)]
    loads = [0] * k

    for num in sorted(test_nums, key=lambda n: durations[n], reverse=True):
        lightest = loads.index(min(loads))
        workers[lightest].append(num)
        loads[lightest] += durations[num]

    occupied = [w for w in workers if w]
    if not occupied:
        return [], 0

    # Local search refinement: move a test from heaviest worker to a lighter worker if it reduces max_load
    if len(occupied) > 1:
        loads_list = [sum(durations[t] for t in w) for w in occupied]
        improved = True
        while improved:
            improved = False
            max_idx = loads_list.index(max(loads_list))
            max_val = loads_list[max_idx]

            for test in sorted(occupied[max_idx], key=lambda t: durations[t]):
                t_dur = durations[test]
                for target_idx in range(len(occupied)):
                    if target_idx == max_idx:
                        continue
                    if loads_list[target_idx] + t_dur < max_val:
                        occupied[max_idx].remove(test)
                        occupied[target_idx].append(test)
                        loads_list[max_idx] -= t_dur
                        loads_list[target_idx] += t_dur
                        improved = True
                        break
                if improved:
                    break

    max_load = max([sum(durations[t] for t in w) for w in occupied])
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

    # Precompute fast tests per file for fallback
    fast_file_tests = {}
    file_to_tests = cov.get("file_to_tests", {})
    for tf, tnums in file_to_tests.items():
        fast_file_tests[tf] = sorted(tnums, key=lambda n: durations.get(n, 0))[:10]

    # 3. Process each commit patch
    commits_dir = DATA_DIR / "commits"
    patch_files = sorted(commits_dir.glob("*.patch"))

    selected_count = 0
    for patch_path in patch_files:
        cid = patch_path.stem
        patch_text = patch_path.read_text(encoding="utf-8")

        test_nums = select_tests_for_patch(patch_text, cov, fast_file_tests)
        workers = optimize_workers(sorted(list(test_nums)), durations)

        selected_count += len(test_nums)
        line = json.dumps({"commit": cid, "workers": workers}, separators=(",", ":"))
        sys.stdout.write(line + "\n")

    sys.stdout.flush()
    print(f"Selector processed {len(patch_files)} commits (total {selected_count} tests selected)", file=sys.stderr)


if __name__ == "__main__":
    main()
