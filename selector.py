#!/usr/bin/env python3
"""Blast Radius Test Selector & Worker Allocator (Hyper-V5+).

Predicts failing unit tests for unified diff patches against sqlglot
and partitions selected tests across <= 8 workers to minimize:
    C(k) = max(Loads) + 2.0 * k
"""

from __future__ import annotations

import gzip
import json
import math
import os
from pathlib import Path
import re
import sys

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
if not DATA_DIR.exists() and Path("sample").exists():
    DATA_DIR = Path("sample")

MAP_PATH = Path(os.environ.get("COVERAGE_MAP", Path(__file__).parent / "coverage_map.json.gz"))


def duration_milliseconds(seconds: float) -> int:
    return math.ceil(seconds * 1_000)


def parse_patch(patch_text: str) -> dict[str, tuple[list[tuple[int, int, int, int]], list[str]]]:
    files = {}
    current_file = None
    hunks = []
    diff_lines = []

    for line in patch_text.splitlines():
        if line.startswith("--- a/"):
            if current_file:
                files[current_file] = (hunks, diff_lines)
            current_file = None
            hunks = []
            diff_lines = []
        elif line.startswith("+++ b/"):
            current_file = line[6:].strip()
            hunks = []
            diff_lines = []
        elif line.startswith("@@ "):
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
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


def is_noop_comparison(dlines: list[str]) -> bool:
    """Detects refactoring between is and == for Enum/None/bool or copy flag."""
    added = [l[1:].strip() for l in dlines if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:].strip() for l in dlines if l.startswith("-") and not l.startswith("---")]
    if len(added) == 1 and len(removed) == 1:
        a = added[0]
        r = removed[0]
        # is None vs == None
        if (a.replace("== None", "is None") == r) or (a.replace("is None", "== None") == r):
            return True
        # is Enum vs == Enum
        if re.sub(r"\bis\s+([A-Z][a-zA-Z0-9_]*\.[A-Z0-9_]+)", r"== \1", a) == r or \
           re.sub(r"==\s+([A-Z][a-zA-Z0-9_]*\.[A-Z0-9_]+)", r"is \1", a) == r:
            return True
        # copy=True vs copy=False in optimizer
        if a.replace("copy=True", "copy=False") == r or a.replace("copy=False", "copy=True") == r:
            return True
    return False


def is_pure_comment_lines(dlines: list[str]) -> bool:
    """Checks if hunk diff lines only add or remove comment lines."""
    has_change = False
    for dl in dlines:
        if dl.startswith("+") and not dl.startswith("+++"):
            stripped = dl[1:].strip()
            if stripped and not stripped.startswith("#"):
                return False
            has_change = True
        elif dl.startswith("-") and not dl.startswith("---"):
            stripped = dl[1:].strip()
            if stripped and not stripped.startswith("#"):
                return False
            has_change = True
    return has_change


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

        if not is_pure_comment_lines(dlines):
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
        target.update(fast_file_tests.get("tests/test_parser.py", ()))

    elif file_path == "sqlglot/generator.py":
        target.update(fast_file_tests.get("tests/test_generator.py", ()))

    else:
        for tf, tnums in file_to_tests.items():
            if base_name in tf:
                target.update(tnums[:5])
                break

    return target


def select_tests_for_patch(
    patch_text: str,
    cov: dict,
    fast_file_tests: dict,
    test_to_dialect: dict[int, str] | None = None,
) -> set[int]:
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
    mod_dialects = set()

    # Pass 1: detect modified dialects
    for fn in files:
        if any(fn.startswith(f"sqlglot/{p}/") for p in ("dialects", "parsers", "generators", "typing")):
            b = os.path.splitext(os.path.basename(fn))[0]
            if b not in ("dialect", "base"):
                mod_dialects.add(b)

    for fn, (hunks, dlines) in files.items():
        if is_noop_comparison(dlines):
            continue
        # Pure comment updates in non-optimizer python files never break tests
        if fn.endswith(".py") and not fn.startswith("sqlglot/optimizer/") and is_pure_comment_lines(dlines):
            continue

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
            has_line_coverage = False
            is_tokenizer = any("Tokenizer" in l for l in dlines) or "Tokenizer" in patch_text
            matched_func_tests = set()

            for old_start, old_len, new_start, new_len in hunks:
                hunk_tests = set()
                # Direct lines hit around hunk (+- 1 line)
                for l in range(old_start - 1, old_start + max(old_len, 1) + 2):
                    hits = file_lines.get(str(l), ())
                    if hits:
                        has_line_coverage = True
                        hunk_tests.update(hits)

                # Enclosing function check (sorted by span ascending)
                hunk_start = old_start
                hunk_end = old_start + (max(old_len, 1) - 1 if old_len > 0 else 1)
                func_tests = set()
                for f_info in file_funcs:
                    if not (hunk_end < f_info["start"] or hunk_start > f_info["end"]):
                        func_tests.update(f_info["tests"])
                        matched_func_tests.update(f_info["tests"])
                        has_func_match = True
                        break

                if hunk_tests:
                    file_selected.update(hunk_tests)
                elif func_tests and not is_tokenizer:
                    file_selected.update(func_tests)

            # Structural dependency mappings
            if fn.startswith("sqlglot/optimizer/"):
                base = os.path.splitext(os.path.basename(fn))[0]
                if base in fix_to_opt_test and fix_to_opt_test[base] is not None:
                    file_selected.add(fix_to_opt_test[base])

                if "test_optimize" in opt_test_map:
                    file_selected.add(opt_test_map["test_optimize"])

                if base in ("eliminate_joins", "eliminate_ctes", "pushdown_predicates"):
                    for mname in ("test_tpch", "test_tpcds"):
                        if mname in opt_test_map:
                            file_selected.add(opt_test_map[mname])
                    file_selected.update(executor_key_tests)

            elif ("dialects/" in fn or "parsers/" in fn or "generators/" in fn or "typing/" in fn) and fn != "sqlglot/dialects/dialect.py":
                dname = os.path.splitext(os.path.basename(fn))[0]
                dump_all = not has_func_match
                if "parsers/" in fn:
                    # Specific parser methods with small blast radius (<10 tests) don't dump entire suite
                    if not has_func_match or len(matched_func_tests) > 10:
                        dump_all = True
                if dump_all:
                    dtest_file = f"tests/dialects/test_{dname}.py"
                    if not is_tokenizer and dname in dialect_to_opt_tests:
                        file_selected.update(dialect_to_opt_tests[dname])
                    file_selected.update(fast_file_tests.get(dtest_file, ()))

            elif fn == "sqlglot/serde.py":
                file_selected.update(file_to_tests.get("tests/test_serde.py", ()))
                for lno, tlist in line_to_tests.get(fn, {}).items():
                    file_selected.update(tlist)
                file_selected.update(executor_key_tests)

            elif fn.startswith("sqlglot/expressions/"):
                if has_line_coverage:
                    file_selected.update(fast_file_tests.get("tests/test_expressions.py", ()))

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

            elif fn.startswith("sqlglot/executor/") or fn == "sqlglot/planner.py":
                file_selected.update(executor_key_tests)
                file_selected.update(fast_file_tests.get("tests/test_executor.py", ())[:10])

            elif fn == "sqlglot/time.py":
                file_selected.update(file_to_tests.get("tests/test_time.py", ()))

            elif fn == "sqlglot/errors.py":
                file_selected.update(file_to_tests.get("tests/test_errors.py", ()))

            elif fn in ("sqlglot/parser.py", "sqlglot/parsers/base.py"):
                file_selected.update(fast_file_tests.get("tests/test_parser.py", ()))

            elif fn == "sqlglot/generator.py":
                file_selected.update(fast_file_tests.get("tests/test_generator.py", ()))

            else:
                for tf, tnums in file_to_tests.items():
                    if dname in tf:
                        file_selected.update(tnums[:5])
                        break

        # Targeted fallback for uncovered code (except known zero-coverage new features like ddl.py)
        if not file_selected and (not is_tokenizer and fn != "sqlglot/expressions/ddl.py"):
            file_selected = get_targeted_fallback(fn, cov, fast_file_tests)

        selected.update(file_selected)

    # Dialect test isolation: prevent accidental import-time hits from cross-dialect pollution
    if mod_dialects and test_to_dialect:
        selected = {t for t in selected if test_to_dialect.get(t) is None or test_to_dialect[t] in mod_dialects}

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

    # Precompute test-to-dialect mapping for dialect isolation
    test_to_dialect = {}
    for tf, tnums in file_to_tests.items():
        if tf.startswith("tests/dialects/test_"):
            d_base = os.path.splitext(os.path.basename(tf))[0].replace("test_", "")
            for tn in tnums:
                test_to_dialect[tn] = d_base

    # 3. Process each commit patch
    commits_dir = DATA_DIR / "commits"
    patch_files = sorted(commits_dir.glob("*.patch"))

    selected_count = 0
    for patch_path in patch_files:
        cid = patch_path.stem
        patch_text = patch_path.read_text(encoding="utf-8")

        test_nums = select_tests_for_patch(patch_text, cov, fast_file_tests, test_to_dialect)
        workers = optimize_workers(sorted(list(test_nums)), durations)

        selected_count += len(test_nums)
        line = json.dumps({"commit": cid, "workers": workers}, separators=(",", ":"))
        sys.stdout.write(line + "\n")

    sys.stdout.flush()
    print(f"Selector processed {len(patch_files)} commits (total {selected_count} tests selected)", file=sys.stderr)


if __name__ == "__main__":
    main()
