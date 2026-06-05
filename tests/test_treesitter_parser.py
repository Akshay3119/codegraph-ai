"""
Extensive tests for the multi-language Tree-sitter extractor.

These tests exercise `ingestion/treesitter_parser.py` across many languages and
edge cases. They are pure (no Neo4j / Qdrant / network): they write small source
files to a temp dir and assert on the extracted entities/relationships.

A language test self-skips if its grammar is not installed in the running
environment, so the suite stays green on minimal installs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingestion.treesitter_parser import (
    EXTENSION_TO_SPEC,
    SUPPORTED_EXTENSIONS,
    ExtractedEntity,
    ExtractedRelationship,
    ParseResult,
    _get_parser,
    _module_qname,
    parse_codebase,
    parse_file,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _qnames(result: ParseResult) -> set[str]:
    return {e.qualified_name for e in result.entities}


def _by_qname(result: ParseResult, qname: str) -> ExtractedEntity:
    matches = [e for e in result.entities if e.qualified_name == qname]
    assert matches, f"entity {qname!r} not found; have {sorted(_qnames(result))}"
    return matches[0]


def _rel_targets(result: ParseResult, rel_type: str) -> set[str]:
    return {r.target_qname for r in result.relationships if r.rel_type == rel_type}


def _has_rel(result: ParseResult, src: str, tgt: str, rel_type: str) -> bool:
    return any(
        r.source_qname == src and r.target_qname == tgt and r.rel_type == rel_type
        for r in result.relationships
    )


def _require(lang: str) -> None:
    if _get_parser(lang) is None:
        pytest.skip(f"tree-sitter grammar for {lang!r} not installed")


# ══════════════════════════════════════════════════════════════════════════════
# Data model / registry
# ══════════════════════════════════════════════════════════════════════════════


def test_supported_extensions_cover_common_languages():
    for ext in (".py", ".js", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".cpp"):
        assert ext in SUPPORTED_EXTENSIONS
        assert ext in EXTENSION_TO_SPEC


def test_module_qname_paths():
    assert _module_qname(Path("a/b/c.py")) == "a.b.c"
    assert _module_qname(Path("pkg/__init__.py")) == "pkg"
    assert _module_qname(Path("main.go")) == "main"
    assert _module_qname(Path("src/app/Service.java")) == "src.app.Service"


def test_dataclasses_are_constructible():
    e = ExtractedEntity(
        entity_type="function",
        qualified_name="m.f",
        file_path="/x/m.py",
        start_line=1,
        end_line=2,
        docstring="",
        source_code="def f(): pass",
    )
    assert e.kind == "" and e.language == ""
    r = ExtractedRelationship("m", "m.f", "DEFINES")
    assert r.rel_type == "DEFINES"


# ══════════════════════════════════════════════════════════════════════════════
# Python
# ══════════════════════════════════════════════════════════════════════════════

PY_SRC = '''\
"""Module level doc."""
import os
import sys as system
from collections import defaultdict, OrderedDict


class Animal:
    """An animal."""

    def __init__(self, name):
        self.name = name

    def speak(self):
        """Make a sound."""
        return make_sound(self.name)


class Dog(Animal):
    def speak(self):
        return bark()


def helper(x):
    return Animal(x)


async def afetch():
    return await helper(1)
'''


def test_python_entities_and_qualified_names(tmp_path):
    _require("python")
    f = _write(tmp_path, "zoo.py", PY_SRC)
    r = parse_file(f, "zoo")

    qn = _qnames(r)
    assert "zoo" in qn
    assert "zoo.Animal" in qn
    assert "zoo.Animal.__init__" in qn
    assert "zoo.Animal.speak" in qn
    assert "zoo.Dog" in qn
    assert "zoo.Dog.speak" in qn
    assert "zoo.helper" in qn
    assert "zoo.afetch" in qn  # async def handled

    assert _by_qname(r, "zoo").entity_type == "module"
    assert _by_qname(r, "zoo.Animal").entity_type == "class"
    assert _by_qname(r, "zoo.helper").entity_type == "function"
    assert _by_qname(r, "zoo.Animal").language == "python"


def test_python_docstrings(tmp_path):
    _require("python")
    f = _write(tmp_path, "zoo.py", PY_SRC)
    r = parse_file(f, "zoo")
    assert _by_qname(r, "zoo").docstring == "Module level doc."
    assert _by_qname(r, "zoo.Animal").docstring == "An animal."
    assert _by_qname(r, "zoo.Animal.speak").docstring == "Make a sound."
    # No docstring -> empty string, never None.
    assert _by_qname(r, "zoo.helper").docstring == ""


def test_python_imports(tmp_path):
    _require("python")
    f = _write(tmp_path, "zoo.py", PY_SRC)
    r = parse_file(f, "zoo")
    targets = _rel_targets(r, "IMPORTS")
    assert "os" in targets
    # from-import records both module and symbols (best-effort).
    assert any("collections" in t or "defaultdict" in t for t in targets)


def test_python_defines_and_calls(tmp_path):
    _require("python")
    f = _write(tmp_path, "zoo.py", PY_SRC)
    r = parse_file(f, "zoo")

    assert _has_rel(r, "zoo", "zoo.Animal", "DEFINES")
    assert _has_rel(r, "zoo.Animal", "zoo.Animal.speak", "DEFINES")

    calls = _rel_targets(r, "CALLS")
    assert "make_sound" in calls
    assert "bark" in calls
    assert "Animal" in calls
    # Call attribution: speak() body calls make_sound from inside the method.
    assert _has_rel(r, "zoo.Animal.speak", "make_sound", "CALLS")


def test_python_line_numbers_are_sane(tmp_path):
    _require("python")
    f = _write(tmp_path, "zoo.py", PY_SRC)
    r = parse_file(f, "zoo")
    for e in r.entities:
        assert e.start_line >= 1
        assert e.end_line >= e.start_line


def test_python_source_code_captured(tmp_path):
    _require("python")
    f = _write(tmp_path, "zoo.py", PY_SRC)
    r = parse_file(f, "zoo")
    helper = _by_qname(r, "zoo.helper")
    assert "def helper" in helper.source_code


# ══════════════════════════════════════════════════════════════════════════════
# JavaScript
# ══════════════════════════════════════════════════════════════════════════════

JS_SRC = """\
import { foo } from "./foo";
import bar from "bar";

export class Widget {
  constructor() { this.init(); }
  render() { return draw(this); }
}

export const handler = (req) => { return process(req); };

function plain() { return 1; }
"""


def test_javascript_extraction(tmp_path):
    _require("javascript")
    f = _write(tmp_path, "app.js", JS_SRC)
    r = parse_file(f, "app")
    qn = _qnames(r)

    assert "app.Widget" in qn
    assert "app.Widget.render" in qn
    assert "app.plain" in qn
    assert "app.handler" in qn  # arrow function bound to const

    assert _by_qname(r, "app.Widget").entity_type == "class"
    assert _by_qname(r, "app.handler").entity_type == "function"

    imports = _rel_targets(r, "IMPORTS")
    assert any("foo" in t for t in imports)
    assert any("bar" in t for t in imports)

    calls = _rel_targets(r, "CALLS")
    assert "draw" in calls
    assert "process" in calls


# ══════════════════════════════════════════════════════════════════════════════
# TypeScript
# ══════════════════════════════════════════════════════════════════════════════

TS_SRC = """\
import { Thing } from "./thing";

export interface Repo {
  find(id: string): Thing;
}

export enum Color { Red, Green }

export class Service implements Repo {
  find(id: string): Thing { return lookup(id); }
}
"""


def test_typescript_interface_enum_class(tmp_path):
    _require("typescript")
    f = _write(tmp_path, "svc.ts", TS_SRC)
    r = parse_file(f, "svc")
    qn = _qnames(r)

    assert "svc.Repo" in qn
    assert "svc.Color" in qn
    assert "svc.Service" in qn
    assert "svc.Service.find" in qn

    # Interfaces/enums map to coarse "class" but keep a fine-grained kind.
    assert _by_qname(r, "svc.Repo").entity_type == "class"
    assert _by_qname(r, "svc.Repo").kind == "interface"
    assert _by_qname(r, "svc.Color").kind == "enum"
    assert "lookup" in _rel_targets(r, "CALLS")


def test_tsx_supported(tmp_path):
    _require("tsx")
    f = _write(tmp_path, "C.tsx", "export const C = () => { return build(); };\n")
    r = parse_file(f, "C")
    assert "C.C" in _qnames(r)
    assert "build" in _rel_targets(r, "CALLS")


# ══════════════════════════════════════════════════════════════════════════════
# Java
# ══════════════════════════════════════════════════════════════════════════════

JAVA_SRC = """\
package app;
import java.util.List;

public class Service {
    public void run() { helper(); }

    interface Handler { void handle(); }

    enum State { ON, OFF }
}
"""


def test_java_extraction(tmp_path):
    _require("java")
    f = _write(tmp_path, "Service.java", JAVA_SRC)
    r = parse_file(f, "Service")
    qn = _qnames(r)

    assert "Service.Service" in qn
    assert "Service.Service.run" in qn
    assert "Service.Service.Handler" in qn
    assert "Service.Service.State" in qn

    assert _by_qname(r, "Service.Service.Handler").kind == "interface"
    assert "java.util.List" in _rel_targets(r, "IMPORTS")
    assert "helper" in _rel_targets(r, "CALLS")


# ══════════════════════════════════════════════════════════════════════════════
# Go
# ══════════════════════════════════════════════════════════════════════════════

GO_SRC = """\
package main

import "fmt"

type Server struct {
    Port int
}

func (s *Server) Start() {
    fmt.Println("up")
}

func main() {
    NewServer()
}
"""


def test_go_extraction(tmp_path):
    _require("go")
    f = _write(tmp_path, "main.go", GO_SRC)
    r = parse_file(f, "main")
    qn = _qnames(r)

    assert "main.Server" in qn  # struct via type_spec
    assert "main.main" in qn
    assert _by_qname(r, "main.Server").entity_type == "class"
    assert any(e.entity_type == "function" and e.qualified_name.endswith("Start") for e in r.entities)

    assert "fmt" in _rel_targets(r, "IMPORTS")
    assert "NewServer" in _rel_targets(r, "CALLS")


# ══════════════════════════════════════════════════════════════════════════════
# Rust
# ══════════════════════════════════════════════════════════════════════════════

RUST_SRC = """\
use std::collections::HashMap;

struct Point { x: i32 }

impl Point {
    fn norm(&self) -> i32 { compute() }
}

fn main() {
    let p = Point { x: 1 };
}
"""


def test_rust_extraction(tmp_path):
    _require("rust")
    f = _write(tmp_path, "lib.rs", RUST_SRC)
    r = parse_file(f, "lib")
    qn = _qnames(r)

    assert "lib.Point" in qn
    assert "lib.main" in qn
    assert any(e.qualified_name.endswith("norm") for e in r.entities)
    assert "compute" in _rel_targets(r, "CALLS")
    assert any("HashMap" in t or "std" in t for t in _rel_targets(r, "IMPORTS"))


# ══════════════════════════════════════════════════════════════════════════════
# C / C++
# ══════════════════════════════════════════════════════════════════════════════


def test_c_function_name_resolution(tmp_path):
    _require("c")
    src = "#include <stdio.h>\nint add(int a, int b) { return compute(a, b); }\n"
    f = _write(tmp_path, "m.c", src)
    r = parse_file(f, "m")
    # C function names live inside a function_declarator; the resolver must dig in.
    assert any(e.entity_type == "function" and e.qualified_name == "m.add" for e in r.entities)


# ══════════════════════════════════════════════════════════════════════════════
# Directory walking, filtering, robustness
# ══════════════════════════════════════════════════════════════════════════════


def test_parse_codebase_aggregates_multiple_languages(tmp_path):
    _require("python")
    _require("go")
    _write(tmp_path, "a.py", "def f():\n    return 1\n")
    _write(tmp_path, "pkg/main.go", "package main\nfunc main() {}\n")
    r = parse_codebase(str(tmp_path))
    langs = {e.language for e in r.entities}
    assert "python" in langs
    assert "go" in langs


def test_parse_codebase_skips_ignored_dirs(tmp_path):
    _require("python")
    _write(tmp_path, "keep.py", "def kept():\n    return 1\n")
    _write(tmp_path, "node_modules/dep.py", "def hidden():\n    return 1\n")
    _write(tmp_path, "__pycache__/cache.py", "def cached():\n    return 1\n")
    _write(tmp_path, ".git/x.py", "def gitfile():\n    return 1\n")
    _write(tmp_path, "venv/lib.py", "def venvfn():\n    return 1\n")

    r = parse_codebase(str(tmp_path))
    qn = _qnames(r)
    assert any(q.endswith("kept") for q in qn)
    assert not any("hidden" in q for q in qn)
    assert not any("cached" in q for q in qn)
    assert not any("gitfile" in q for q in qn)
    assert not any("venvfn" in q for q in qn)


def test_unsupported_extension_returns_empty(tmp_path):
    f = _write(tmp_path, "notes.txt", "just some text, not code")
    r = parse_file(f)
    assert r.entities == []
    assert r.relationships == []


def test_syntax_errors_do_not_crash(tmp_path):
    _require("python")
    # Tree-sitter is error-tolerant: broken code yields a partial tree, not a raise.
    f = _write(tmp_path, "broken.py", "def oops(:\n    return\nclass \n")
    r = parse_file(f, "broken")
    assert isinstance(r, ParseResult)
    # At minimum the module entity is always produced.
    assert any(e.entity_type == "module" for e in r.entities)


def test_empty_file_yields_module_only(tmp_path):
    _require("python")
    f = _write(tmp_path, "empty.py", "")
    r = parse_file(f, "empty")
    assert [e.entity_type for e in r.entities] == ["module"]


def test_entity_types_are_coarse_only(tmp_path):
    _require("python")
    _require("typescript")
    _write(tmp_path, "a.py", PY_SRC)
    _write(tmp_path, "b.ts", TS_SRC)
    r = parse_codebase(str(tmp_path))
    assert {e.entity_type for e in r.entities} <= {"module", "class", "function"}


def test_relationship_types_are_known(tmp_path):
    _require("python")
    _write(tmp_path, "a.py", PY_SRC)
    r = parse_codebase(str(tmp_path))
    assert {rel.rel_type for rel in r.relationships} <= {"IMPORTS", "DEFINES", "CALLS"}


def test_parse_codebase_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_codebase(str(tmp_path / "does-not-exist"))


def test_nested_scope_qualified_names(tmp_path):
    _require("python")
    src = "class Outer:\n    class Inner:\n        def deep(self):\n            return 1\n"
    f = _write(tmp_path, "n.py", src)
    r = parse_file(f, "n")
    assert "n.Outer.Inner.deep" in _qnames(r)


# ══════════════════════════════════════════════════════════════════════════════
# Backward-compatibility: old import path still works
# ══════════════════════════════════════════════════════════════════════════════


def test_backward_compatible_imports_from_parser():
    from ingestion.parser import (  # noqa: F401
        ExtractedEntity as PE,
        ExtractedRelationship as PR,
        ParseResult as PPR,
        Neo4jWriter,
        QdrantWriter,
        parse_codebase as pc,
        run_ingestion,
    )

    assert PE is ExtractedEntity
    assert pc is parse_codebase
