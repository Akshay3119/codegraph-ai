from __future__ import annotations

"""
==============================================================================
Multi-Language Code Extraction — treesitter_parser.py
==============================================================================
This module replaces the previous Python-only `ast` extractor with a
**Tree-sitter** based one. Tree-sitter parses many programming languages into
concrete syntax trees, which lets the ingestion pipeline build a knowledge
graph for polyglot codebases (Python, JavaScript/TypeScript, Java, Go, Rust,
C/C++, C#, Ruby, PHP, ...).

The public surface is intentionally **identical** to the old `ast` extractor so
the rest of the pipeline (Neo4j / Qdrant writers, the agent, the frontend) keeps
working unchanged:

    - ExtractedEntity        (entity_type: "module" | "class" | "function")
    - ExtractedRelationship  (rel_type: "IMPORTS" | "DEFINES" | "CALLS")
    - ParseResult
    - parse_codebase(root_dir) -> ParseResult

Design notes:
  - Each language has a small, declarative `LanguageSpec` describing which
    Tree-sitter node types represent classes, functions, imports and calls.
  - A single generic tree walker maintains a scope stack to build dotted
    qualified names (e.g. "pkg.module.ClassName.method"), mirroring the old
    behaviour.
  - Finer-grained kinds (interface, struct, enum, method, ...) are preserved on
    the entity via the `kind` field and stored as a node property, while the
    coarse `entity_type` stays one of module/class/function so existing Cypher,
    graph colors and UI filters keep working.
  - Languages are loaded lazily and any grammar that isn't installed is skipped
    gracefully rather than crashing the whole ingest.
==============================================================================
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("ingestion.treesitter_parser")


# ══════════════════════════════════════════════════════════════════════════════
# Public data model (kept compatible with the old ast-based extractor)
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class ExtractedEntity:
    """
    A single structural entity discovered while walking a syntax tree.

    Fields:
        entity_type : Coarse type used everywhere downstream — one of
                      "module", "class", "function".
        qualified_name : Dot-separated fully-qualified name.
        file_path : Absolute path to the source file.
        start_line : Starting line number (1-indexed).
        end_line : Ending line number (1-indexed).
        docstring : Docstring / leading doc comment (empty string if absent).
        source_code : Raw source text of the entity (capped for embedding).
        parent_qname : Qualified name of the containing entity (None for modules).
        language : Tree-sitter language name (e.g. "python", "typescript").
        kind : Fine-grained kind (e.g. "interface", "struct", "method") for
               richer context; defaults to the coarse entity_type.
    """

    entity_type: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    docstring: str
    source_code: str
    parent_qname: str | None = None
    language: str = ""
    kind: str = ""


@dataclass
class ExtractedRelationship:
    """A directed edge between two entities (IMPORTS | DEFINES | CALLS)."""

    source_qname: str
    target_qname: str
    rel_type: str


@dataclass
class ParseResult:
    """Aggregated output from a full codebase parse."""

    entities: list[ExtractedEntity] = field(default_factory=list)
    relationships: list[ExtractedRelationship] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
# Language registry
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class LanguageSpec:
    """
    Declarative description of how to extract entities for one language.

    The node-type names below are Tree-sitter grammar node types. Where a
    construct does not exist for a language the corresponding set is simply left
    empty.

    Fields:
        name : Tree-sitter language name passed to the language pack.
        class_types : Node types treated as class-like definitions
                      (classes, interfaces, structs, enums, traits, ...).
        function_types : Node types treated as function/method definitions.
        import_types : Node types representing import/use/include statements.
        call_types : Node types representing call expressions.
        binding_types : Node types that may bind an anonymous function to a
                        name (e.g. `const f = () => {}` in JS/TS).
        lambda_types : Anonymous function node types found as the value of a
                       binding (arrow functions, function expressions).
    """

    name: str
    class_types: frozenset[str] = frozenset()
    function_types: frozenset[str] = frozenset()
    import_types: frozenset[str] = frozenset()
    call_types: frozenset[str] = frozenset()
    binding_types: frozenset[str] = frozenset()
    lambda_types: frozenset[str] = frozenset()


# Fine-grained kind labels keyed by node type (for nicer UI / context only).
_KIND_BY_NODE_TYPE: dict[str, str] = {
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "enum_item": "enum",
    "struct_item": "struct",
    "struct_specifier": "struct",
    "union_specifier": "union",
    "trait_item": "trait",
    "impl_item": "impl",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
    "type_spec": "type",
    "method_declaration": "method",
    "method_definition": "method",
    "constructor_declaration": "constructor",
    "singleton_method": "method",
    "module": "module",
    "mod_item": "module",
}

# Node types that carry a usable identifier for a definition's name.
_IDENTIFIER_NODE_TYPES: frozenset[str] = frozenset(
    {
        "identifier",
        "type_identifier",
        "field_identifier",
        "property_identifier",
        "constant",
        "constant_identifier",
        "scoped_identifier",
        "namespace_identifier",
        "word",
    }
)


# ── Per-language specs ────────────────────────────────────────────────────────

_PYTHON = LanguageSpec(
    name="python",
    class_types=frozenset({"class_definition"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"import_statement", "import_from_statement"}),
    call_types=frozenset({"call"}),
)

_JS_CLASS = frozenset({"class_declaration", "class"})
_JS_FUNC = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "method_definition",
    }
)
_JS_CALL = frozenset({"call_expression", "new_expression"})
_JS_BINDING = frozenset({"variable_declarator", "public_field_definition", "pair"})
_JS_LAMBDA = frozenset({"arrow_function", "function_expression", "function", "generator_function"})

_JAVASCRIPT = LanguageSpec(
    name="javascript",
    class_types=_JS_CLASS,
    function_types=_JS_FUNC,
    import_types=frozenset({"import_statement"}),
    call_types=_JS_CALL,
    binding_types=_JS_BINDING,
    lambda_types=_JS_LAMBDA,
)

_TS_CLASS = frozenset(
    {
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "enum_declaration",
    }
)
_TS_FUNC = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "method_definition",
        "function_signature",
        "method_signature",
        "abstract_method_signature",
    }
)

_TYPESCRIPT = LanguageSpec(
    name="typescript",
    class_types=_TS_CLASS,
    function_types=_TS_FUNC,
    import_types=frozenset({"import_statement"}),
    call_types=_JS_CALL,
    binding_types=_JS_BINDING,
    lambda_types=_JS_LAMBDA,
)

_TSX = LanguageSpec(
    name="tsx",
    class_types=_TS_CLASS,
    function_types=_TS_FUNC,
    import_types=frozenset({"import_statement"}),
    call_types=_JS_CALL,
    binding_types=_JS_BINDING,
    lambda_types=_JS_LAMBDA,
)

_JAVA = LanguageSpec(
    name="java",
    class_types=frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "annotation_type_declaration",
        }
    ),
    function_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"method_invocation", "object_creation_expression"}),
)

_GO = LanguageSpec(
    name="go",
    class_types=frozenset({"type_spec"}),
    function_types=frozenset({"function_declaration", "method_declaration"}),
    import_types=frozenset({"import_spec"}),
    call_types=frozenset({"call_expression"}),
)

_RUST = LanguageSpec(
    name="rust",
    class_types=frozenset(
        {"struct_item", "enum_item", "trait_item", "impl_item", "mod_item", "union_item"}
    ),
    function_types=frozenset({"function_item"}),
    import_types=frozenset({"use_declaration"}),
    call_types=frozenset({"call_expression", "macro_invocation"}),
)

_RUBY = LanguageSpec(
    name="ruby",
    class_types=frozenset({"class", "module"}),
    function_types=frozenset({"method", "singleton_method"}),
    import_types=frozenset(),
    call_types=frozenset({"call", "method_call"}),
)

_C = LanguageSpec(
    name="c",
    class_types=frozenset({"struct_specifier", "union_specifier", "enum_specifier"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"preproc_include"}),
    call_types=frozenset({"call_expression"}),
)

_CPP = LanguageSpec(
    name="cpp",
    class_types=frozenset(
        {"class_specifier", "struct_specifier", "union_specifier", "enum_specifier"}
    ),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"preproc_include"}),
    call_types=frozenset({"call_expression", "new_expression"}),
)

_CSHARP = LanguageSpec(
    name="csharp",
    class_types=frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "struct_declaration",
            "enum_declaration",
            "record_declaration",
        }
    ),
    function_types=frozenset({"method_declaration", "constructor_declaration", "local_function_statement"}),
    import_types=frozenset({"using_directive"}),
    call_types=frozenset({"invocation_expression", "object_creation_expression"}),
)

_PHP = LanguageSpec(
    name="php",
    class_types=frozenset(
        {"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"}
    ),
    function_types=frozenset({"function_definition", "method_declaration"}),
    import_types=frozenset({"namespace_use_declaration"}),
    call_types=frozenset({"function_call_expression", "member_call_expression", "object_creation_expression"}),
)


# Extension → LanguageSpec. Drives both file discovery and parsing.
EXTENSION_TO_SPEC: dict[str, LanguageSpec] = {
    ".py": _PYTHON,
    ".pyi": _PYTHON,
    ".js": _JAVASCRIPT,
    ".jsx": _JAVASCRIPT,
    ".mjs": _JAVASCRIPT,
    ".cjs": _JAVASCRIPT,
    ".ts": _TYPESCRIPT,
    ".mts": _TYPESCRIPT,
    ".cts": _TYPESCRIPT,
    ".tsx": _TSX,
    ".java": _JAVA,
    ".go": _GO,
    ".rs": _RUST,
    ".rb": _RUBY,
    ".c": _C,
    ".h": _C,
    ".cpp": _CPP,
    ".cc": _CPP,
    ".cxx": _CPP,
    ".hpp": _CPP,
    ".hh": _CPP,
    ".cs": _CSHARP,
    ".php": _PHP,
}

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(EXTENSION_TO_SPEC)

# Directories we never descend into during discovery.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        "venv",
        ".venv",
        "node_modules",
        "dist",
        "build",
        "target",
        "vendor",
        ".git",
        ".idea",
        ".vscode",
        ".mypy_cache",
        ".pytest_cache",
    }
)


# ══════════════════════════════════════════════════════════════════════════════
# Tree-sitter parser cache
# ══════════════════════════════════════════════════════════════════════════════

_PARSER_CACHE: dict[str, object] = {}
_UNAVAILABLE: set[str] = set()


def _get_parser(language_name: str):
    """Return a cached Tree-sitter parser for *language_name*, or None if the
    grammar is unavailable in the installed language pack.

    We pair the upstream ``tree_sitter.Parser`` with the grammar objects shipped
    by ``tree_sitter_language_pack``. This keeps us on the modern, byte-oriented
    Parser API regardless of any binding bundled by the language pack.
    """
    if language_name in _PARSER_CACHE:
        return _PARSER_CACHE[language_name]
    if language_name in _UNAVAILABLE:
        return None
    try:
        from tree_sitter import Parser
        from tree_sitter_language_pack import get_language

        parser = Parser(get_language(language_name))
    except Exception as exc:  # grammar missing / load failure
        logger.warning("Tree-sitter grammar unavailable for '%s': %s", language_name, exc)
        _UNAVAILABLE.add(language_name)
        return None
    _PARSER_CACHE[language_name] = parser
    return parser


# ══════════════════════════════════════════════════════════════════════════════
# Generic tree walker
# ══════════════════════════════════════════════════════════════════════════════


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _find_name_node(node):
    """Locate the identifier node naming a definition, across language shapes.

    Tries (in order): the `name` field, the `declarator` field (C/C++/Rust),
    then the first direct identifier-like child."""
    nn = node.child_by_field_name("name")
    if nn is not None:
        return nn

    decl = node.child_by_field_name("declarator")
    if decl is not None:
        if decl.type in _IDENTIFIER_NODE_TYPES:
            return decl
        sub = _find_name_node(decl)
        if sub is not None:
            return sub

    for child in node.children:
        if child.type in _IDENTIFIER_NODE_TYPES:
            return child

    for child in node.children:
        if "declarator" in child.type:
            sub = _find_name_node(child)
            if sub is not None:
                return sub
    return None


def _definition_name(node, source: bytes) -> str | None:
    name_node = _find_name_node(node)
    if name_node is None:
        return None
    name = _text(name_node, source).strip()
    # Keep only the last identifier for scoped names like `a::b::c` or `pkg.Type`.
    for sep in ("::", "."):
        if sep in name:
            name = name.split(sep)[-1]
    return name or None


def _python_docstring_from_body(body, source: bytes) -> str:
    for child in body.children:
        if child.type == "comment":
            continue
        # Some grammar versions expose the docstring string directly in the
        # block; others wrap it in an expression_statement.
        if child.type in ("string", "concatenated_string"):
            return _clean_docstring(_text(child, source))
        if child.type == "expression_statement":
            inner = child.children[0] if child.children else None
            if inner is not None and inner.type in ("string", "concatenated_string"):
                return _clean_docstring(_text(inner, source))
            return ""
        return ""
    return ""


def _python_docstring(node, source: bytes) -> str:
    """Best-effort docstring extraction for Python definitions/modules."""
    if node.type == "module":
        return _python_docstring_from_body(node, source)
    body = node.child_by_field_name("body")
    if body is None:
        return ""
    return _python_docstring_from_body(body, source)


def _clean_docstring(raw: str) -> str:
    s = raw.strip()
    for q in ('"""', "'''", '"', "'", "r'''", 'r"""'):
        if s.startswith(q):
            s = s[len(q) :]
            break
    for q in ('"""', "'''", '"', "'"):
        if s.endswith(q):
            s = s[: -len(q)]
            break
    return s.strip()


def _call_target(node, source: bytes, spec: LanguageSpec) -> str | None:
    """Resolve a call expression to a dotted callee name."""
    fn = node.child_by_field_name("function")
    if fn is None:
        fn = node.child_by_field_name("name")  # Java method_invocation
    if fn is None:
        fn = node.child_by_field_name("constructor")  # some object-creation nodes
    if fn is None:
        # Fall back to the first identifier-like child.
        for child in node.children:
            if child.type in _IDENTIFIER_NODE_TYPES or "identifier" in child.type:
                fn = child
                break
    if fn is None:
        return None
    text = _text(fn, source).strip()
    # Take the first line, strip generic/type args, collapse whitespace.
    text = text.splitlines()[0].strip()
    if not text or any(c in text for c in "(){}[]<>"):
        # Likely an inline expression rather than a clean name.
        return None
    return text or None


def _import_targets(node, source: bytes, spec: LanguageSpec) -> list[str]:
    """Best-effort extraction of imported module/symbol names."""
    targets: list[str] = []

    # Common explicit fields first.
    for field_name in ("module_name", "name", "path", "source"):
        fn = node.child_by_field_name(field_name)
        if fn is not None:
            txt = _text(fn, source).strip().strip("\"';`")
            if txt:
                targets.append(txt)

    if targets:
        return targets

    # Otherwise gather string literals and dotted/identifier names in the node.
    def _collect(n):
        if n.type in ("string", "string_literal", "interpreted_string_literal"):
            targets.append(_text(n, source).strip().strip("\"';`"))
        elif n.type in ("dotted_name", "scoped_identifier", "identifier", "namespace_identifier"):
            targets.append(_text(n, source).strip())
        else:
            for c in n.children:
                _collect(c)

    _collect(node)
    # De-dup while preserving order, drop empties.
    seen: set[str] = set()
    cleaned: list[str] = []
    for t in targets:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            cleaned.append(t)
    return cleaned or ([_text(node, source).strip().splitlines()[0]] if node.children else [])


class _TreeWalker:
    """Recursively walks a Tree-sitter tree, emitting entities/relationships."""

    # Source caps mirror the previous ast extractor's behaviour.
    _CLASS_CAP = 3000
    _FUNCTION_CAP = 3000
    _MODULE_CAP = 2000

    def __init__(self, file_path: str, module_qname: str, source: bytes, spec: LanguageSpec):
        self.file_path = file_path
        self.module_qname = module_qname
        self.source = source
        self.spec = spec
        self.result = ParseResult()
        self._scope_stack: list[str] = [module_qname]

    def _scope(self) -> str:
        return self._scope_stack[-1]

    def run(self, root) -> ParseResult:
        module_doc = ""
        if self.spec.name == "python":
            module_doc = _python_docstring(root, self.source)

        self.result.entities.append(
            ExtractedEntity(
                entity_type="module",
                qualified_name=self.module_qname,
                file_path=self.file_path,
                start_line=1,
                end_line=(root.end_point[0] + 1),
                docstring=module_doc,
                source_code=_text(root, self.source)[: self._MODULE_CAP],
                language=self.spec.name,
                kind="module",
            )
        )
        for child in root.children:
            self._walk(child)
        return self.result

    def _walk(self, node) -> None:
        ntype = node.type

        if ntype in self.spec.class_types:
            self._emit_definition(node, entity_type="class", cap=self._CLASS_CAP)
            return

        if ntype in self.spec.function_types:
            self._emit_definition(node, entity_type="function", cap=self._FUNCTION_CAP)
            return

        if ntype in self.spec.binding_types:
            if self._emit_bound_lambda(node):
                return

        if ntype in self.spec.import_types:
            for target in _import_targets(node, self.source, self.spec):
                self.result.relationships.append(
                    ExtractedRelationship(self._scope(), target, "IMPORTS")
                )
            # Imports can contain nested names but no further defs we care about.
            for child in node.children:
                self._walk(child)
            return

        if ntype in self.spec.call_types:
            target = _call_target(node, self.source, self.spec)
            if target:
                self.result.relationships.append(
                    ExtractedRelationship(self._scope(), target, "CALLS")
                )
            for child in node.children:
                self._walk(child)
            return

        for child in node.children:
            self._walk(child)

    def _emit_definition(self, node, entity_type: str, cap: int) -> None:
        name = _definition_name(node, self.source)
        if not name:
            # Anonymous definition (e.g. default-exported class): still recurse.
            for child in node.children:
                self._walk(child)
            return

        qname = f"{self._scope()}.{name}"
        docstring = _python_docstring(node, self.source) if self.spec.name == "python" else ""
        kind = _KIND_BY_NODE_TYPE.get(node.type, entity_type)

        self.result.entities.append(
            ExtractedEntity(
                entity_type=entity_type,
                qualified_name=qname,
                file_path=self.file_path,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                docstring=docstring,
                source_code=_text(node, self.source)[:cap],
                parent_qname=self._scope(),
                language=self.spec.name,
                kind=kind,
            )
        )
        self.result.relationships.append(
            ExtractedRelationship(self._scope(), qname, "DEFINES")
        )

        self._scope_stack.append(qname)
        for child in node.children:
            self._walk(child)
        self._scope_stack.pop()

    def _emit_bound_lambda(self, node) -> bool:
        """Handle `const f = () => {}` / `foo = function() {}` style definitions.

        Returns True if it consumed the node as a function definition."""
        value = node.child_by_field_name("value")
        if value is None:
            return False
        if value.type not in self.spec.lambda_types:
            return False
        name = _definition_name(node, self.source)
        if not name:
            return False

        qname = f"{self._scope()}.{name}"
        self.result.entities.append(
            ExtractedEntity(
                entity_type="function",
                qualified_name=qname,
                file_path=self.file_path,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                docstring="",
                source_code=_text(node, self.source)[: self._FUNCTION_CAP],
                parent_qname=self._scope(),
                language=self.spec.name,
                kind="function",
            )
        )
        self.result.relationships.append(
            ExtractedRelationship(self._scope(), qname, "DEFINES")
        )

        self._scope_stack.append(qname)
        for child in value.children:
            self._walk(child)
        self._scope_stack.pop()
        return True


# ══════════════════════════════════════════════════════════════════════════════
# File + directory parsing
# ══════════════════════════════════════════════════════════════════════════════


def _module_qname(relative: Path) -> str:
    parts = list(relative.parts)
    last = parts[-1]
    if last == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = Path(last).stem
    return ".".join(parts) if parts else relative.stem


def parse_file(file_path: str | Path, module_qname: str | None = None) -> ParseResult:
    """Parse a single source file into entities + relationships.

    Returns an empty ParseResult if the language is unsupported or its grammar
    is not installed."""
    path = Path(file_path)
    spec = EXTENSION_TO_SPEC.get(path.suffix.lower())
    if spec is None:
        return ParseResult()

    parser = _get_parser(spec.name)
    if parser is None:
        return ParseResult()

    try:
        source = path.read_bytes()
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return ParseResult()

    try:
        tree = parser.parse(source)
    except Exception as exc:
        logger.warning("Tree-sitter failed to parse %s: %s", path, exc)
        return ParseResult()

    qname = module_qname or path.stem
    walker = _TreeWalker(str(path), qname, source, spec)
    return walker.run(tree.root_node)


def parse_codebase(root_dir: str) -> ParseResult:
    """
    Walk a directory tree, parse every supported source file with Tree-sitter,
    and return aggregated structural entities and relationships.

    Args:
        root_dir: Absolute or relative path to the codebase root.

    Returns:
        ParseResult containing all discovered entities and relationships.
    """
    root = Path(root_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Codebase root not found: {root}")

    aggregated = ParseResult()
    files = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    logger.info("Discovered %d source files in %s", len(files), root)

    lang_counts: dict[str, int] = {}
    for src_file in files:
        parts = src_file.relative_to(root).parts
        if any(p.startswith(".") or p in _SKIP_DIRS for p in parts):
            continue

        relative = src_file.relative_to(root)
        result = parse_file(src_file, _module_qname(relative))
        if result.entities:
            spec = EXTENSION_TO_SPEC.get(src_file.suffix.lower())
            if spec is not None:
                lang_counts[spec.name] = lang_counts.get(spec.name, 0) + 1
        aggregated.entities.extend(result.entities)
        aggregated.relationships.extend(result.relationships)

    if lang_counts:
        langs = ", ".join(f"{k}={v}" for k, v in sorted(lang_counts.items()))
        logger.info("Parsed files by language: %s", langs)
    logger.info(
        "Extraction complete: %d entities, %d relationships",
        len(aggregated.entities),
        len(aggregated.relationships),
    )
    return aggregated
