from vpa.engines.repair import _tool_handler_glob, _tool_handler_grep


def test_grep_recursive_match(tmp_path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "foo.c").write_text("int value = 1;\nvoid func(void) {}\n")
    (src / "bar.h").write_text("#define VALUE 1\nvoid func(void);\n")
    (src / "data.txt").write_text("int value = 2;\n")  # .txt, not .c/.h, should be skipped

    r = _tool_handler_grep({"pattern": "func", "path": "src"}, tmp_path)
    assert r["count"] == 2
    files = {m["file"] for m in r["matches"]}
    assert "src/foo.c" in files
    assert "src/bar.h" in files


def test_grep_no_match(tmp_path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "foo.c").write_text("int x = 1;\n")

    r = _tool_handler_grep({"pattern": "nonexistent_symbol", "path": "src"}, tmp_path)
    assert r["count"] == 0
    assert r["matches"] == []


def test_grep_rejects_outside_path(tmp_path):
    src = tmp_path / "src"
    src.mkdir(parents=True)

    r = _tool_handler_grep({"pattern": "test", "path": "/etc/passwd"}, tmp_path)
    assert "error" in r
    assert "outside repository" in r["error"].lower()


def test_grep_context_lines(tmp_path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "foo.c").write_text("int a = 0;\nint target = 1;\nint b = 2;\n")

    r = _tool_handler_grep({"pattern": "target", "path": "src", "context_lines": 1}, tmp_path)
    assert r["count"] >= 1
    lines_text = " ".join(m["text"] for m in r["matches"])
    assert "a" in lines_text or "b" in lines_text


def test_grep_on_single_file(tmp_path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "foo.c").write_text("int value = 1;\nvoid func(void) {}\n")

    r = _tool_handler_grep({"pattern": "value", "path": "src/foo.c"}, tmp_path)
    assert r["count"] == 1
    assert r["matches"][0]["file"] == "src/foo.c"


def test_glob_finds_files(tmp_path):
    (tmp_path / "src/dynarec/rv64").mkdir(parents=True)
    (tmp_path / "src/dynarec/rv64/dynarec_rv64_00.c").write_text("")
    (tmp_path / "src/dynarec/rv64/dynarec_rv64_00.h").write_text("")
    (tmp_path / "src/dynarec/rv64/rv64_emit.c").write_text("")
    (tmp_path / "src/dynarec/sw64_core3").mkdir(parents=True)
    (tmp_path / "src/dynarec/sw64_core3/dynarec_sw64_00.c").write_text("")

    r = _tool_handler_glob(
        {"pattern": "**/*.c", "path": "src/dynarec/rv64"},
        workspace=tmp_path,
    )
    assert r["count"] == 2
    assert "dynarec_rv64_00.c" in r["matches"]


def test_glob_missing_directory(tmp_path):
    r = _tool_handler_glob(
        {"pattern": "*.c", "path": "nonexistent"},
        workspace=tmp_path,
    )
    assert "error" in r
