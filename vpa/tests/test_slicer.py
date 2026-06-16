"""Tests for slicer — commit/file/hunk fallback, dependency ordering, stable IDs.

No real API calls. Fixtures are local temporary git repos.
Phase 9: Slicer correctness audit and hardening.

Contract notes:

  1. Dependency ordering source of truth:
     topoligical_sort_files reads LOCAL file content (via local_path / f).
     This is deliberate: we order slices for the LOCAL porting process, so
     local dependency structure is what matters.  When local files don't
     exist (e.g. newly added upstream files), the heuristic fallback
     (priority + lexicographic sort) applies deterministically.
     This is LOCAL-REPO HEURISTIC ORDERING, not upstream semantic
     dependency ordering.

  2. HUNK level is file-scoped large-change fallback:
     When a single-file diff exceeds FILE_THRESHOLD_LINES (200), the slicer
     produces one HUNK-level slice for the entire file.  It does NOT split
     per-@@ hunk.  Multiple @@ hunks within one file still produce a single
     file-scoped HUNK slice.

  3. Thresholds count raw diff text lines:
     count_diff_lines returns len(diff.split("\n")) — ALL lines in the diff
     text including headers, context, and control lines.  This is an
     approximate diff-size proxy, NOT a semantic changed-line count.
"""

import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase

from vpa.slicer import (
    COMMIT_THRESHOLD_FILES,
    COMMIT_THRESHOLD_LINES,
    FILE_THRESHOLD_LINES,
    SliceLevel,
    count_diff_lines,
    get_commit_diff,
    get_commit_files,
    parse_diff_per_file,
    slice_commits,
    topological_sort_files,
)


def _init_repo(path: Path):
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, capture_output=True,
    )


def _commit_all(path: Path, message: str):
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message], cwd=path, capture_output=True,
    )


def _rev_parse(path: Path, ref="HEAD"):
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=path,
        capture_output=True, text=True,
    ).stdout.strip()


def _create_fixture_repos(base_dir: Path):
    upstream = base_dir / "upstream"
    local = base_dir / "local"
    for d in [upstream, local]:
        d.mkdir()
        _init_repo(d)

    (upstream / "f.c").write_text("int x = 1;\n")
    _commit_all(upstream, "initial")
    old = _rev_parse(upstream)

    (upstream / "f.c").write_text("int x = 1;\nint y = 2;\n")
    _commit_all(upstream, "add y")
    new = _rev_parse(upstream)

    (local / "f.c").write_text("int x = 1;\n")
    _commit_all(local, "initial")
    return upstream, local, old, new


def _create_many_file_fixture(base_dir: Path, file_count: int):
    upstream = base_dir / "upstream"
    local = base_dir / "local"
    for d in [upstream, local]:
        d.mkdir()
        _init_repo(d)

    (upstream / "base.c").write_text("int base = 0;\n")
    _commit_all(upstream, "initial")
    old = _rev_parse(upstream)

    for i in range(file_count):
        (upstream / f"feat{i}.c").write_text(f"int feat{i} = {i};\n")
    _commit_all(upstream, f"add {file_count} files")
    new = _rev_parse(upstream)

    (local / "base.c").write_text("int base = 0;\n")
    _commit_all(local, "initial")
    return upstream, local, old, new


def _create_large_file_fixture(base_dir: Path, num_lines: int):
    """Single file with num_lines of content.

    Diff lines ≈ num_lines + 5 (headers + trailing).  Use num_lines=300
    to exceed both COMMIT_THRESHOLD_LINES (300) and FILE_THRESHOLD_LINES (200),
    producing HUNK-level slices.
    """
    upstream = base_dir / "upstream"
    local = base_dir / "local"
    for d in [upstream, local]:
        d.mkdir()
        _init_repo(d)

    (upstream / "f.c").write_text("int x = 0;\n")
    _commit_all(upstream, "initial")
    old = _rev_parse(upstream)

    lines = [f"int x = {i};\n" for i in range(num_lines)]
    (upstream / "f.c").write_text("".join(lines))
    _commit_all(upstream, "big change")
    new = _rev_parse(upstream)

    (local / "f.c").write_text("int x = 0;\n")
    _commit_all(local, "initial")
    return upstream, local, old, new


def _create_multi_file_large_diff_fixture(
    base_dir: Path, file_count: int, lines_per_file: int,
):
    """Multiple files each with lines_per_file lines of content.

    Each per-file diff ≈ lines_per_file + 6 (headers + context).
    Use file_count=2, lines_per_file=160 so total raw diff text
    > COMMIT_THRESHOLD_LINES (300) but each per-file raw diff text
    < FILE_THRESHOLD_LINES (200) → FILE-level slices.
    """
    upstream = base_dir / "upstream"
    local = base_dir / "local"
    for d in [upstream, local]:
        d.mkdir()
        _init_repo(d)

    for i in range(file_count):
        (upstream / f"f{i}.c").write_text(f"int x_{i} = 0;\n")
    _commit_all(upstream, "initial")
    old = _rev_parse(upstream)

    for i in range(file_count):
        lines = [f"int x_{i} = {j};\n" for j in range(lines_per_file)]
        (upstream / f"f{i}.c").write_text("".join(lines))
    _commit_all(upstream, "multi-file large change")
    new = _rev_parse(upstream)

    # Local matches upstream initial
    for i in range(file_count):
        (local / f"f{i}.c").write_text(f"int x_{i} = 0;\n")
    _commit_all(local, "initial")
    return upstream, local, old, new


# ═══════════════════════════════════════════════════════════════════════
# 1. Commit-level slice below threshold
# ═══════════════════════════════════════════════════════════════════════


class TestCommitLevelSlice(TestCase):
    """A small commit should remain one commit-level slice."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.old, self.new = \
            _create_fixture_repos(self.tmp)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_small_commit_stays_commit_level(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0].level, SliceLevel.COMMIT)

    def test_small_commit_work_items(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        items = slices[0].to_work_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "file")
        self.assertEqual(items[0]["upstream_file"], "f.c")
        self.assertEqual(items[0]["local_file"], "f.c")

    def test_small_commit_context_includes_files(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        ctx = slices[0].context
        self.assertIn("f.c", ctx)

    def test_commit_with_small_diff_below_threshold(self):
        """Verify diff lines are below both thresholds."""
        full_diff = get_commit_diff(str(self.upstream), self.new)
        files = get_commit_files(str(self.upstream), self.new)
        dl = count_diff_lines(full_diff)
        self.assertLessEqual(dl, COMMIT_THRESHOLD_LINES)
        self.assertLessEqual(len(files), COMMIT_THRESHOLD_FILES)


class TestCommitLevelSliceNoLocalDir(TestCase):
    """Commit-level slicing also works without a local_dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, _, self.old, self.new = \
            _create_fixture_repos(self.tmp)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_no_local_dir_fallback(self):
        slices = list(slice_commits(str(self.upstream), self.old, self.new))
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0].level, SliceLevel.COMMIT)
        self.assertIsNone(slices[0].file_diff)
        # to_work_items still works
        items = slices[0].to_work_items()
        self.assertEqual(len(items), 1)


class TestDescribe(TestCase):
    """Slice.describe() produces correct labels."""

    def test_commit_describe(self):
        from vpa.slicer import Slice
        s = Slice(level=SliceLevel.COMMIT, label="c", commit_sha="a" * 40)
        self.assertIn("commit", s.describe())
        self.assertIn("aaaaaaaa", s.describe())

    def test_file_describe(self):
        from vpa.slicer import Slice
        s = Slice(level=SliceLevel.FILE, label="f", commit_sha="a" * 40,
                  file_path="src/foo.c")
        self.assertIn("file", s.describe())
        self.assertIn("src/foo.c", s.describe())

    def test_hunk_describe(self):
        from vpa.slicer import Slice
        s = Slice(level=SliceLevel.HUNK, label="h", commit_sha="a" * 40,
                  file_path="src/bar.c")
        self.assertIn("hunk", s.describe())
        self.assertIn("src/bar.c", s.describe())


# ═══════════════════════════════════════════════════════════════════════
# 2. File-level fallback
# ═══════════════════════════════════════════════════════════════════════


class TestFileLevelFallbackByFileCount(TestCase):
    """A commit exceeding the file count threshold splits into file-level slices."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.old, self.new = \
            _create_many_file_fixture(self.tmp, COMMIT_THRESHOLD_FILES + 1)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_many_files_splits_to_file_slices(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        self.assertGreater(len(slices), 1)
        for sl in slices:
            self.assertEqual(sl.level, SliceLevel.FILE)

    def test_file_count_triggers_fallback(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        self.assertEqual(
            len(slices), COMMIT_THRESHOLD_FILES + 1,
            "Each changed file becomes a slice",
        )

    def test_file_slices_have_unique_paths(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        paths = [s.file_path for s in slices]
        self.assertEqual(len(paths), len(set(paths)))

    def test_file_slices_have_file_diff(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        for sl in slices:
            self.assertIsNotNone(sl.file_diff)
            self.assertGreater(len(sl.file_diff or ""), 0)

    def test_file_slices_to_work_items(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        for sl in slices:
            items = sl.to_work_items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["kind"], "file")
            self.assertIn(sl.file_path, items[0]["id"])

    def test_file_slices_work_item_id_stable(self):
        slices1 = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        slices2 = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        ids1 = [wi["id"] for sl in slices1 for wi in sl.to_work_items()]
        ids2 = [wi["id"] for sl in slices2 for wi in sl.to_work_items()]
        self.assertEqual(ids1, ids2)


class TestFileLevelFallbackByDiffSize(TestCase):
    """A commit exceeding the line threshold splits into file-level slices.

    Uses 2 files, each with ~160 content lines → per-file diff ~166 lines
    (below FILE_THRESHOLD_LINES=200) but total diff ~332 > COMMIT_THRESHOLD_LINES=300.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.old, self.new = \
            _create_multi_file_large_diff_fixture(self.tmp, 2, 160)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_large_diff_triggers_file_fallback(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        self.assertEqual(len(slices), 2)
        for sl in slices:
            self.assertEqual(sl.level, SliceLevel.FILE)

    def test_file_slices_have_diff_context(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        for sl in slices:
            assert sl.file_diff is not None
            self.assertIn("int x", sl.file_diff)

    def test_file_work_item_ids_have_file_names(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        for sl in slices:
            items = sl.to_work_items()
            self.assertIn(sl.file_path or "", items[0]["id"])


# ═══════════════════════════════════════════════════════════════════════
# 3. Hunk-level fallback
# ═══════════════════════════════════════════════════════════════════════


class TestHunkLevelFallback(TestCase):
    """A large single-file change produces one file-scoped HUNK slice.

    Current contract: HUNK level means file-scoped large-change fallback.
    The slicer does NOT split per-@@ hunk.  One large file → one HUNK slice.

    Uses 1 file with 350 lines → total raw diff text ~356
    > COMMIT_THRESHOLD_LINES (300) AND file diff ~356
    > FILE_THRESHOLD_LINES (200) → HUNK slice.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.old, self.new = \
            _create_large_file_fixture(self.tmp, 350)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_large_file_produces_hunk_slice(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0].level, SliceLevel.HUNK)

    def test_hunk_slice_has_file_path(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        self.assertEqual(slices[0].file_path, "f.c")

    def test_hunk_slice_has_file_diff(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        self.assertIsNotNone(slices[0].file_diff)
        self.assertGreater(len(slices[0].file_diff or ""), 0)

    def test_hunk_slice_context_is_hunk_style(self):
        """Hunk slices get a short context message, not the full diff."""
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        self.assertIn("(", slices[0].context)
        self.assertIn("lines in diff", slices[0].context)

    def test_hunk_work_item_kind_is_hunk(self):
        slices = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        items = slices[0].to_work_items()
        self.assertEqual(items[0]["kind"], "hunk")

    def test_hunk_work_item_id_is_stable(self):
        slices1 = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        slices2 = list(slice_commits(
            str(self.upstream), self.old, self.new, str(self.local),
        ))
        ids1 = [wi["id"] for sl in slices1 for wi in sl.to_work_items()]
        ids2 = [wi["id"] for sl in slices2 for wi in sl.to_work_items()]
        self.assertEqual(ids1, ids2)

    def test_multiple_hunks_in_one_file_still_one_slice(self):
        """Multiple @@ hunks in one diff still produce one slice, not one-per-hunk.

        This confirms current behavior: slicing is per-file, not per-@@-hunk.
        Even at COMMIT level, a file with many dispersed changes produces one slice.
        """
        tmp2 = Path(tempfile.mkdtemp())
        try:
            upstream = tmp2 / "upstream"
            local = tmp2 / "local"
            for d in [upstream, local]:
                d.mkdir()
                _init_repo(d)

            # 300-line file, change 3 regions with 10-line gaps between
            # This guarantees git produces 3+ @@ hunks
            initial_lines = [f"int x_{i} = {i};\n" for i in range(300)]
            (upstream / "multi.c").write_text("".join(initial_lines))
            _commit_all(upstream, "initial")
            old = _rev_parse(upstream)

            modified = list(initial_lines)
            for i in range(10, 100):
                modified[i] = f"int x_{i} = {i * 10};\n"
            for i in range(110, 200):
                modified[i] = f"int x_{i} = {i * 100};\n"
            for i in range(210, 300):
                modified[i] = f"int x_{i} = {i * 1000};\n"
            (upstream / "multi.c").write_text("".join(modified))
            _commit_all(upstream, "three dispersed changes")
            new = _rev_parse(upstream)

            (local / "multi.c").write_text("".join(initial_lines))
            _commit_all(local, "initial")

            # Verify the diff actually has multiple @@ hunks
            raw_diff = get_commit_diff(str(upstream), new)
            hunk_count = raw_diff.count("@@ -")
            self.assertGreaterEqual(
                hunk_count, 2,
                f"Test requires multiple @@ hunks, got {hunk_count}",
            )

            slices = list(slice_commits(str(upstream), old, new, str(local)))
            self.assertEqual(
                len(slices), 1,
                "Multiple @@ hunks in one file should still produce one slice",
            )
        finally:
            subprocess.run(["rm", "-rf", str(tmp2)])


class TestThresholdBoundaries(TestCase):
    """Exact boundary behavior for COMMIT and FILE thresholds."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_at_exact_file_count_threshold_stays_commit_level(self):
        """Exactly COMMIT_THRESHOLD_FILES files → still a commit-level slice."""
        up, loc, old, new = _create_many_file_fixture(
            self.tmp, COMMIT_THRESHOLD_FILES,
        )
        slices = list(slice_commits(str(up), old, new, str(loc)))
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0].level, SliceLevel.COMMIT)

    def test_one_over_file_count_threshold_splits(self):
        """COMMIT_THRESHOLD_FILES + 1 files → file-level slices."""
        tmp2 = Path(tempfile.mkdtemp())
        try:
            up, loc, old, new = _create_many_file_fixture(
                tmp2, COMMIT_THRESHOLD_FILES + 1,
            )
            slices = list(slice_commits(str(up), old, new, str(loc)))
            self.assertGreater(len(slices), 1)
            for sl in slices:
                self.assertEqual(sl.level, SliceLevel.FILE)
        finally:
            subprocess.run(["rm", "-rf", str(tmp2)])

    def test_file_below_hunk_threshold_not_hunk(self):
        """FILE_THRESHOLD_LINES - 1 content lines → raw diff ~205
        < COMMIT_THRESHOLD_LINES (300) → COMMIT-level slice.
        Confirms that sub-hunk-threshold files do not reach HUNK level."""
        up, loc, old, new = _create_large_file_fixture(
            self.tmp, FILE_THRESHOLD_LINES - 1,
        )
        slices = list(slice_commits(str(up), old, new, str(loc)))
        self.assertGreater(len(slices), 0)
        self.assertNotEqual(
            slices[0].level, SliceLevel.HUNK,
            "Below-threshold file should not produce HUNK slice",
        )


# ═══════════════════════════════════════════════════════════════════════
# 4. Deterministic file ordering
# ═══════════════════════════════════════════════════════════════════════


class TestDeterministicFileOrdering(TestCase):
    """Given the same input, file slice order must be stable across runs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_many_files_slice_order_stable(self):
        up, loc, old, new = _create_many_file_fixture(
            self.tmp, COMMIT_THRESHOLD_FILES + 3,
        )
        slices1 = list(slice_commits(str(up), old, new, str(loc)))
        slices2 = list(slice_commits(str(up), old, new, str(loc)))
        paths1 = [s.file_path for s in slices1]
        paths2 = [s.file_path for s in slices2]
        self.assertEqual(paths1, paths2)

    def test_single_repo_gives_stable_order(self):
        """Run slice_commits twice on the same repos and compare."""
        up, loc, old, new = _create_many_file_fixture(
            self.tmp, COMMIT_THRESHOLD_FILES + 5,
        )
        result_a = list(slice_commits(str(up), old, new, str(loc)))
        result_b = list(slice_commits(str(up), old, new, str(loc)))
        labels_a = [sl.label for sl in result_a]
        labels_b = [sl.label for sl in result_b]
        self.assertEqual(labels_a, labels_b)


class TestTopologicalSortDeterminism(TestCase):
    """topological_sort_files returns deterministic order for same input.

    This is LOCAL-REPO HEURISTIC ORDERING, not upstream semantic dependency
    ordering.  The function reads LOCAL file content (via the given paths)
    to detect includes/imports.  When files don't exist locally, the
    deterministic priority + lexicographic fallback applies.
    """

    def test_empty_list(self):
        self.assertEqual(topological_sort_files([]), [])

    def test_single_file(self):
        self.assertEqual(topological_sort_files(["a.c"]), ["a.c"])

    def test_two_files_same_priority_lexicographic(self):
        files = ["z_last.c", "a_first.c"]
        result = topological_sort_files(files)
        self.assertEqual(result, ["a_first.c", "z_last.c"])

    def test_multiple_calls_same_result(self):
        files = ["z.c", "a.h", "m_test.c", "b.c"]
        r1 = topological_sort_files(files)
        r2 = topological_sort_files(files)
        self.assertEqual(r1, r2)

    def test_header_file_priority(self):
        """Headers (.h) get priority 1, implementations (.c) get priority 3."""
        files = ["impl.c", "header.h", "main.c"]
        result = topological_sort_files(files)
        self.assertEqual(result[0], "header.h")

    def test_test_file_last_priority(self):
        """Test files get priority 5, go to end."""
        files = ["core.c", "test_core.c", "header.h"]
        result = topological_sort_files(files)
        self.assertIn("test_core.c", result[-1] if "test_core.c" else result)

    def test_directory_priority(self):
        """Files in lib/, include/ get priority 2."""
        files = ["cmd/main.c", "lib/util.c", "include/api.h", "test/test_core.c"]
        result = topological_sort_files(files)
        self.assertIn("include/api.h", result[:2])
        self.assertIn("test/test_core.c", result[-1])


# ═══════════════════════════════════════════════════════════════════════
# 5. Dependency ordering
# ═══════════════════════════════════════════════════════════════════════


class TestDependencyOrdering(TestCase):
    """Dependency analysis reads LOCAL files. Fallback is deterministic.

    Dependency source of truth: LOCAL file content.  The slicer opens
    paths under local_dir to detect includes/imports.  This is a deliberate
    approximation: we order slices for the LOCAL porting process.

    When local files don't exist (e.g. newly added upstream files),
    open() fails, dependencies are empty, and the deterministic
    priority + lexicographic heuristic applies.
    """

    def test_nonexistent_files_sorted_by_heuristic(self):
        """Files that don't exist on disk get deterministic priority + lexicographic sort.

        This exercises the fallback path: open() raises FileNotFoundError,
        dependency set is empty, and sorting uses _priority() + filename.
        """
        files = ["z_last.c", "a_first.h", "m_mid.c"]
        result = topological_sort_files(files)
        self.assertEqual(len(result), 3)
        # .h first (priority 1), then .c sorted lexicographically
        self.assertEqual(result[0], "a_first.h")

    def test_fallback_never_crashes(self):
        """All kinds of edge paths handled."""
        cases = [
            [],
            ["a.c"],
            ["no_ext", "b.c"],
            ["deeply/nested/file.c"],
            ["a.h", "b.h", "c.h"],
        ]
        for files in cases:
            result = topological_sort_files(files)
            self.assertEqual(len(result), len(files))

    def test_dependency_analysis_with_local_files(self):
        """When local files exist with includes, sort respects deps.

        This confirms the LOCAL-REPO HEURISTIC ORDERING contract:
        topological_sort_files reads file content from disk to determine
        include/import dependencies for ordering.
        """
        d = Path(tempfile.mkdtemp())
        try:
            (d / "header.h").write_text("#ifndef H\n#define H\n#endif\n")
            (d / "impl.c").write_text('#include "header.h"\nint main(void) { return 0; }\n')

            result = topological_sort_files(
                [str(d / "impl.c"), str(d / "header.h")],
            )
            self.assertEqual(len(result), 2)
            self.assertEqual(
                result[0], str(d / "header.h"),
                "Header should come before implementation",
            )
        finally:
            subprocess.run(["rm", "-rf", str(d)])

    def test_dependency_cycle_resolved_deterministically(self):
        """Circular includes fall back to priority sort."""
        d = Path(tempfile.mkdtemp())
        try:
            (d / "a.h").write_text('#include "b.h"\n')
            (d / "b.h").write_text('#include "a.h"\n')
            result = topological_sort_files(
                [str(d / "b.h"), str(d / "a.h")],
            )
            self.assertEqual(len(result), 2)
            # Should not crash, deterministic result
        finally:
            subprocess.run(["rm", "-rf", str(d)])


# ═══════════════════════════════════════════════════════════════════════
# 6. Rename/delete/add/binary behavior
# ═══════════════════════════════════════════════════════════════════════


class TestEdgeCases(TestCase):
    """Handle rename, delete, add, and binary files safely."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def _make_upstream_local(self):
        upstream = self.tmp / "upstream"
        local = self.tmp / "local"
        for d in [upstream, local]:
            d.mkdir()
            _init_repo(d)
        return upstream, local

    def test_deleted_file_does_not_crash(self):
        upstream, local = self._make_upstream_local()

        (upstream / "f.c").write_text("int x = 1;\n")
        _commit_all(upstream, "initial")
        old = _rev_parse(upstream)

        (upstream / "f.c").unlink()
        _commit_all(upstream, "delete f.c")
        new = _rev_parse(upstream)

        (local / "f.c").write_text("int x = 1;\n")
        _commit_all(local, "initial")

        slices = list(slice_commits(str(upstream), old, new, str(local)))
        self.assertGreater(len(slices), 0)
        self.assertEqual(slices[0].commit_sha, new)

    def test_added_file_does_not_crash(self):
        upstream, local = self._make_upstream_local()

        (upstream / "base.c").write_text("int x = 1;\n")
        _commit_all(upstream, "initial")
        old = _rev_parse(upstream)

        (upstream / "new.c").write_text("int y = 2;\n")
        _commit_all(upstream, "add new.c")
        new = _rev_parse(upstream)

        (local / "base.c").write_text("int x = 1;\n")
        _commit_all(local, "initial")

        slices = list(slice_commits(str(upstream), old, new, str(local)))
        self.assertEqual(len(slices), 1)
        self.assertIn(slices[0].level, (SliceLevel.COMMIT, SliceLevel.FILE))

    def test_binary_file_does_not_crash(self):
        upstream, local = self._make_upstream_local()

        (upstream / "f.c").write_text("int x = 1;\n")
        _commit_all(upstream, "initial")
        old = _rev_parse(upstream)

        (upstream / "f.c").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        _commit_all(upstream, "replace with binary")
        new = _rev_parse(upstream)

        (local / "f.c").write_text("int x = 1;\n")
        _commit_all(local, "initial")

        slices = list(slice_commits(str(upstream), old, new, str(local)))
        self.assertGreater(len(slices), 0)

    def test_multi_file_commit_with_delete_and_add(self):
        upstream, local = self._make_upstream_local()

        (upstream / "a.c").write_text("int a = 1;\n")
        (upstream / "b.c").write_text("int b = 2;\n")
        _commit_all(upstream, "initial")
        old = _rev_parse(upstream)

        (upstream / "a.c").unlink()
        (upstream / "c.c").write_text("int c = 3;\n")
        (upstream / "b.c").write_text("int b = 42;\n")
        _commit_all(upstream, "delete a, add c, modify b")
        new = _rev_parse(upstream)

        (local / "a.c").write_text("int a = 1;\n")
        (local / "b.c").write_text("int b = 2;\n")
        _commit_all(local, "initial")

        slices = list(slice_commits(str(upstream), old, new, str(local)))
        self.assertGreater(len(slices), 0)


# ═══════════════════════════════════════════════════════════════════════
# 7. Stable IDs
# ═══════════════════════════════════════════════════════════════════════


class TestStableIDs(TestCase):
    """Slice/work-item IDs must be stable across repeated runs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_commit_level_ids_stable(self):
        up, loc, old, new = _create_fixture_repos(self.tmp)

        r1 = list(slice_commits(str(up), old, new, str(loc)))
        r2 = list(slice_commits(str(up), old, new, str(loc)))

        ids1 = [wi["id"] for sl in r1 for wi in sl.to_work_items()]
        ids2 = [wi["id"] for sl in r2 for wi in sl.to_work_items()]
        self.assertEqual(ids1, ids2)

    def test_file_level_ids_stable(self):
        tmp2 = Path(tempfile.mkdtemp())
        try:
            up, loc, old, new = _create_many_file_fixture(
                tmp2, COMMIT_THRESHOLD_FILES + 3,
            )
            r1 = list(slice_commits(str(up), old, new, str(loc)))
            r2 = list(slice_commits(str(up), old, new, str(loc)))

            ids1 = [wi["id"] for sl in r1 for wi in sl.to_work_items()]
            ids2 = [wi["id"] for sl in r2 for wi in sl.to_work_items()]
            self.assertEqual(ids1, ids2)
        finally:
            subprocess.run(["rm", "-rf", str(tmp2)])

    def test_hunk_level_ids_stable(self):
        tmp2 = Path(tempfile.mkdtemp())
        try:
            up, loc, old, new = _create_large_file_fixture(
                tmp2, 350,
            )
            r1 = list(slice_commits(str(up), old, new, str(loc)))
            r2 = list(slice_commits(str(up), old, new, str(loc)))

            ids1 = [wi["id"] for sl in r1 for wi in sl.to_work_items()]
            ids2 = [wi["id"] for sl in r2 for wi in sl.to_work_items()]
            self.assertEqual(ids1, ids2)
        finally:
            subprocess.run(["rm", "-rf", str(tmp2)])

    def test_no_duplicate_ids_within_commit(self):
        up, loc, old, new = _create_many_file_fixture(
            self.tmp, COMMIT_THRESHOLD_FILES + 3,
        )
        slices = list(slice_commits(str(up), old, new, str(loc)))
        ids = [wi["id"] for sl in slices for wi in sl.to_work_items()]
        self.assertEqual(len(ids), len(set(ids)),
                         "Duplicate work item IDs within commit")

    def test_ids_contain_short_sha_and_path(self):
        up, loc, old, new = _create_many_file_fixture(
            self.tmp, COMMIT_THRESHOLD_FILES + 2,
        )
        slices = list(slice_commits(str(up), old, new, str(loc)))
        for sl in slices:
            for wi in sl.to_work_items():
                self.assertIn(new[:8], wi["id"])
                self.assertIn(sl.file_path or "", wi["id"])


# ═══════════════════════════════════════════════════════════════════════
# 8. parse_diff_per_file
# ═══════════════════════════════════════════════════════════════════════


class TestParseDiffPerFile(TestCase):
    """parse_diff_per_file correctly splits multi-file diffs."""

    def test_empty_diff(self):
        self.assertEqual(parse_diff_per_file(""), {})

    def test_single_file_diff(self):
        diff = (
            "diff --git a/f.c b/f.c\n"
            "index abc..def\n"
            "--- a/f.c\n"
            "+++ b/f.c\n"
            "@@ -1 +1,2 @@\n"
            " old\n"
            "+new\n"
        )
        result = parse_diff_per_file(diff)
        self.assertEqual(len(result), 1)
        self.assertIn("f.c", result)
        self.assertIn("old", result["f.c"])
        self.assertIn("+new", result["f.c"])

    def test_multi_file_diff(self):
        diff = (
            "diff --git a/a.c b/a.c\n"
            "index a..b\n"
            "--- a/a.c\n"
            "+++ b/a.c\n"
            "@@ -1 +1,2 @@\n"
            " old\n"
            "+new\n"
            "diff --git a/b.c b/b.c\n"
            "index c..d\n"
            "--- a/b.c\n"
            "+++ b/b.c\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        result = parse_diff_per_file(diff)
        self.assertEqual(len(result), 2)
        self.assertIn("a.c", result)
        self.assertIn("b.c", result)

    def test_diff_with_rename(self):
        diff = (
            "diff --git a/old.c b/new.c\n"
            "similarity index 100%\n"
            "rename from old.c\n"
            "rename to new.c\n"
        )
        result = parse_diff_per_file(diff)
        self.assertIn("new.c", result)

    def test_diff_with_binary(self):
        diff = (
            "diff --git a/img.png b/img.png\n"
            "index abc..def\n"
            "Binary files differ\n"
        )
        result = parse_diff_per_file(diff)
        self.assertIn("img.png", result)

    def test_parse_roundtrip_with_real_diff(self):
        """Verify parse_diff_per_file matches get_commit_files for a real repo."""
        tmp = Path(tempfile.mkdtemp())
        try:
            upstream = tmp / "upstream"
            upstream.mkdir()
            _init_repo(upstream)

            (upstream / "a.c").write_text("int a = 1;\n")
            (upstream / "b.c").write_text("int b = 2;\n")
            _commit_all(upstream, "initial")

            (upstream / "a.c").write_text("int a = 42;\n")
            (upstream / "c.c").write_text("int c = 3;\n")
            _commit_all(upstream, "modify a, add c")
            new = _rev_parse(upstream)

            diff = get_commit_diff(str(upstream), new)
            files = get_commit_files(str(upstream), new)
            per_file = parse_diff_per_file(diff)

            for f in files:
                self.assertIn(f, per_file,
                              f"File {f} from diff-tree not found in parsed diff")
        finally:
            subprocess.run(["rm", "-rf", str(tmp)])

    def test_parse_handles_no_trailing_newline(self):
        """Diff without trailing newline (edge case)."""
        diff = (
            "diff --git a/f.c b/f.c\n"
            "--- a/f.c\n"
            "+++ b/f.c\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new"
        )
        result = parse_diff_per_file(diff)
        self.assertEqual(len(result), 1)
        self.assertIn("f.c", result)
        self.assertIn("+new", result["f.c"])


# ═══════════════════════════════════════════════════════════════════════
# 9. count_diff_lines
# ═══════════════════════════════════════════════════════════════════════


class TestCountDiffLines(TestCase):
    """count_diff_lines returns correct line counts for diff text."""

    def test_empty_diff(self):
        self.assertEqual(count_diff_lines(""), 1)

    def test_single_line_diff(self):
        self.assertEqual(count_diff_lines("single line\n"), 2)

    def test_multi_line_diff(self):
        diff = "line1\nline2\nline3\n"
        self.assertEqual(count_diff_lines(diff), 4)

    def test_with_trailing_newline(self):
        diff = "a\nb\n"
        self.assertEqual(count_diff_lines(diff), 3)


# ═══════════════════════════════════════════════════════════════════════
# 10. to_work_items contract
# ═══════════════════════════════════════════════════════════════════════


class TestToWorkItemsContract(TestCase):
    """to_work_items produces data shaped for ledger init_work_items."""

    def test_commit_level_items_have_required_fields(self):
        from vpa.slicer import Slice
        s = Slice(
            level=SliceLevel.COMMIT, label="c", commit_sha="a" * 40,
            files=["src/foo.c", "src/bar.c"],
        )
        items = s.to_work_items()
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertIn("id", item)
            self.assertIn("kind", item)
            self.assertIn("upstream_file", item)
            self.assertIn("local_file", item)
            self.assertEqual(item["kind"], "file")

    def test_file_level_item_matches_slice(self):
        from vpa.slicer import Slice
        s = Slice(
            level=SliceLevel.FILE, label="f", commit_sha="a" * 40,
            file_path="src/foo.c",
        )
        items = s.to_work_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["upstream_file"], "src/foo.c")
        self.assertEqual(items[0]["local_file"], "src/foo.c")

    def test_hunk_level_item_matches_slice(self):
        from vpa.slicer import Slice
        s = Slice(
            level=SliceLevel.HUNK, label="h", commit_sha="a" * 40,
            file_path="src/bar.c",
        )
        items = s.to_work_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "hunk")
        self.assertEqual(items[0]["upstream_file"], "src/bar.c")
        self.assertEqual(items[0]["local_file"], "src/bar.c")
        self.assertIn("hunk", items[0]["id"])
