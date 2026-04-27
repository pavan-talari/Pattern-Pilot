"""Import-following context extractor.

When PP reviews changed files, this module:
1. Parses Python imports from each changed file
2. Resolves import paths to source files in the target project
3. Extracts contract-relevant definitions (signatures, constants, classes, layout comments)
4. Returns extracted context as read-only reference for the reviewer

Only follows imports 1 level deep. Skips stdlib/third-party imports.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Standard library top-level module names (subset — covers the common ones)
_STDLIB_MODULES = frozenset({
    "abc", "aifc", "argparse", "array", "ast", "asyncio", "atexit",
    "base64", "binascii", "builtins", "calendar", "cgi", "cmd",
    "codecs", "collections", "colorsys", "compileall", "concurrent",
    "configparser", "contextlib", "copy", "copyreg", "csv", "ctypes",
    "curses", "dataclasses", "datetime", "decimal", "difflib", "dis",
    "email", "enum", "errno", "faulthandler", "fcntl", "filecmp",
    "fileinput", "fnmatch", "fractions", "ftplib", "functools", "gc",
    "getopt", "getpass", "gettext", "glob", "gzip", "hashlib", "heapq",
    "hmac", "html", "http", "imaplib", "importlib", "inspect", "io",
    "ipaddress", "itertools", "json", "keyword", "linecache", "locale",
    "logging", "lzma", "mailbox", "math", "mimetypes", "mmap",
    "multiprocessing", "numbers", "operator", "os", "pathlib", "pdb",
    "pickle", "pickletools", "pipes", "pkgutil", "platform", "plistlib",
    "poplib", "posixpath", "pprint", "profile", "pstats", "py_compile",
    "pydoc", "queue", "quopri", "random", "re", "readline", "reprlib",
    "rlcompleter", "runpy", "sched", "secrets", "select", "selectors",
    "shelve", "shlex", "shutil", "signal", "site", "smtplib", "sndhdr",
    "socket", "socketserver", "sqlite3", "ssl", "stat", "statistics",
    "string", "stringprep", "struct", "subprocess", "sunau", "symtable",
    "sys", "sysconfig", "syslog", "tabnanny", "tarfile", "tempfile",
    "termios", "test", "textwrap", "threading", "time", "timeit",
    "tkinter", "token", "tokenize", "tomllib", "trace", "traceback",
    "tracemalloc", "tty", "turtle", "types", "typing", "unicodedata",
    "unittest", "urllib", "uuid", "venv", "warnings", "wave",
    "weakref", "webbrowser", "wsgiref", "xml", "xmlrpc", "zipapp",
    "zipfile", "zipimport", "zlib",
    # typing extensions
    "typing_extensions",
    # common third-party (skip these too)
    "pydantic", "pydantic_settings", "fastapi", "uvicorn", "sqlalchemy",
    "alembic", "httpx", "openai", "anthropic", "pytest", "requests",
    "numpy", "pandas", "scipy", "matplotlib", "seaborn", "plotly",
    "celery", "redis", "boto3", "botocore", "docker", "click", "typer",
    "rich", "starlette", "jinja2", "mako", "aiohttp", "aiofiles",
    "asyncpg", "psycopg2", "pymongo", "motor", "flask", "django",
    "werkzeug", "gunicorn", "pillow", "PIL",
})

# Regex for layout/section comments
_LAYOUT_COMMENT_RE = re.compile(
    r"^#\s*(?:[─═╌╍┄┅]{3,}|[Ll]ayout\s*:|[Ss]chema\s*:|[Ff]ormat\s*:|[Pp]ath\s*:)",
)

# Regex for ALL_CAPS constants
_CONSTANT_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

# Max chars of extracted context per file
_MAX_EXTRACT_CHARS = 3000


@dataclass
class ImportInfo:
    """A single import statement parsed from source."""

    module: str  # e.g., "myapp.utils" or ".models"
    names: list[str] = field(default_factory=list)  # e.g., ["fetch_user", "UserModel"]
    is_relative: bool = False
    level: int = 0  # for relative imports: number of dots


class ImportParser:
    """Parse Python imports from source code using ast."""

    @staticmethod
    def parse(source: str) -> list[ImportInfo]:
        """Extract imports from Python source. Returns empty list on parse failure."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            logger.debug("Could not parse source for imports (syntax error)")
            return []

        imports: list[ImportInfo] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in _STDLIB_MODULES:
                        imports.append(ImportInfo(module=alias.name))

            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue
                top = node.module.split(".")[0]
                level = node.level or 0
                # Skip stdlib/third-party (only for absolute imports)
                if level == 0 and top in _STDLIB_MODULES:
                    continue
                names = [a.name for a in (node.names or []) if a.name != "*"]
                imports.append(ImportInfo(
                    module=node.module,
                    names=names,
                    is_relative=level > 0,
                    level=level,
                ))

        return imports


class ImportResolver:
    """Resolve import module paths to filesystem paths within a project."""

    def __init__(self, repo_root: str) -> None:
        self.root = Path(repo_root)

    def resolve(self, imp: ImportInfo, source_file: str) -> Path | None:
        """Resolve an import to a file path. Returns None if not found in repo."""
        if imp.is_relative:
            return self._resolve_relative(imp, source_file)
        return self._resolve_absolute(imp.module)

    def _resolve_absolute(self, module: str) -> Path | None:
        """Resolve absolute import: try module.py, module/__init__.py."""
        parts = module.split(".")
        # Try as a file
        candidate = self.root / Path(*parts).with_suffix(".py")
        if candidate.is_file():
            return candidate
        # Try as a package
        candidate = self.root / Path(*parts) / "__init__.py"
        if candidate.is_file():
            return candidate
        # Try partial (module might be a name inside a package)
        if len(parts) > 1:
            candidate = self.root / Path(*parts[:-1]).with_suffix(".py")
            if candidate.is_file():
                return candidate
        return None

    def _resolve_relative(self, imp: ImportInfo, source_file: str) -> Path | None:
        """Resolve relative import from the source file's package."""
        source = Path(source_file)
        # Go up `level` directories from the source file's directory
        base = source.parent
        for _ in range(imp.level - 1):
            base = base.parent

        parts = imp.module.split(".") if imp.module else []
        candidate = base / Path(*parts).with_suffix(".py") if parts else None
        if candidate and candidate.is_file():
            return candidate
        if parts:
            candidate = base / Path(*parts) / "__init__.py"
            if candidate and candidate.is_file():
                return candidate
        return None


class DefinitionExtractor:
    """Extract contract-relevant definitions from a Python source file.

    Extracts:
    - Function/method signatures with docstrings
    - Class definitions with annotated fields
    - Module-level constants (ALL_CAPS)
    - Layout/schema/path comments
    """

    @staticmethod
    def extract(source: str, file_path: str, imported_names: list[str] | None = None) -> str:
        """Extract targeted definitions from source.

        If imported_names is provided, only extract definitions matching those names.
        Otherwise, extract all contract-relevant definitions.
        """
        lines = source.splitlines()
        parts: list[str] = []
        parts.append(f"# Contract context from: {file_path}")
        parts.append("")

        # 1. Extract layout/section comments from raw source
        layout_comments = DefinitionExtractor._extract_layout_comments(lines)
        if layout_comments:
            parts.append("# Layout / schema comments:")
            parts.extend(layout_comments)
            parts.append("")

        # 2. Parse AST for definitions
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # Fall back to regex-based extraction if AST fails
            constants = DefinitionExtractor._extract_constants_regex(lines)
            if constants:
                parts.extend(constants)
            result = "\n".join(parts)
            return result[:_MAX_EXTRACT_CHARS]

        names_set = set(imported_names) if imported_names else None

        # 3. Module-level constants
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and _CONSTANT_RE.match(target.id):
                        if names_set and target.id not in names_set:
                            continue
                        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                        parts.append(f"L{node.lineno}: {line.strip()}")

        # 4. Functions and classes
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if names_set and node.name not in names_set:
                    continue
                sig = DefinitionExtractor._format_function(node, lines)
                if sig:
                    parts.append(sig)

            elif isinstance(node, ast.ClassDef):
                if names_set and node.name not in names_set:
                    continue
                cls = DefinitionExtractor._format_class(node, lines)
                if cls:
                    parts.append(cls)

        result = "\n".join(parts)
        if len(result) > _MAX_EXTRACT_CHARS:
            result = result[:_MAX_EXTRACT_CHARS] + "\n# ... (truncated)"
        return result

    @staticmethod
    def _extract_layout_comments(lines: list[str]) -> list[str]:
        """Extract layout/schema/path comments from source lines."""
        results: list[str] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if _LAYOUT_COMMENT_RE.match(stripped):
                results.append(f"L{i}: {stripped}")
                # Include the next few comment lines for context
                for j in range(i, min(i + 4, len(lines))):
                    next_line = lines[j].strip()
                    if next_line.startswith("#") and not _LAYOUT_COMMENT_RE.match(next_line):
                        results.append(f"L{j + 1}: {next_line}")
                    else:
                        break
        return results

    @staticmethod
    def _extract_constants_regex(lines: list[str]) -> list[str]:
        """Fallback: extract ALL_CAPS constants via regex."""
        results: list[str] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "=" in stripped:
                name = stripped.split("=", 1)[0].strip()
                if _CONSTANT_RE.match(name):
                    results.append(f"L{i}: {stripped}")
        return results

    @staticmethod
    def _format_function(
        node: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]
    ) -> str:
        """Format a function signature with docstring excerpt."""
        # Reconstruct signature from AST
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        args_str = DefinitionExtractor._format_args(node.args)
        returns = ""
        if node.returns:
            ret_line = lines[node.returns.lineno - 1] if node.returns.lineno <= len(lines) else ""
            # Try to get return annotation from source
            if "->" in ret_line:
                returns = " -> " + ret_line.split("->", 1)[1].split(":")[0].strip()

        sig = f"L{node.lineno}: {prefix} {node.name}({args_str}){returns}:"

        # Get docstring
        docstring = ast.get_docstring(node)
        if docstring:
            first_line = docstring.strip().split("\n")[0][:120]
            sig += f'\n    """{first_line}"""'

        return sig

    @staticmethod
    def _format_class(node: ast.ClassDef, lines: list[str]) -> str:
        """Format a class definition with fields and docstring."""
        bases = ", ".join(
            lines[node.lineno - 1].split("(", 1)[1].rsplit(")", 1)[0].strip().split(",")[0:3]
        ) if "(" in (lines[node.lineno - 1] if node.lineno <= len(lines) else "") else ""

        header = f"L{node.lineno}: class {node.name}" + (f"({bases})" if bases else "") + ":"

        parts = [header]

        # Docstring
        docstring = ast.get_docstring(node)
        if docstring:
            first_line = docstring.strip().split("\n")[0][:120]
            parts.append(f'    """{first_line}"""')

        # Annotated fields (class-level)
        for child in node.body:
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                line = lines[child.lineno - 1] if child.lineno <= len(lines) else ""
                parts.append(f"    L{child.lineno}: {line.strip()}")

        return "\n".join(parts)

    @staticmethod
    def _format_args(args: ast.arguments) -> str:
        """Format function arguments concisely."""
        parts: list[str] = []
        all_args = args.args + args.posonlyargs
        defaults_offset = len(all_args) - len(args.defaults)

        for i, arg in enumerate(all_args):
            s = arg.arg
            if arg.annotation:
                # Just use the arg name with annotation hint
                s += ": ..."
            if i >= defaults_offset:
                s += " = ..."
            parts.append(s)

        if args.vararg:
            parts.append(f"*{args.vararg.arg}")
        for kw in args.kwonlyargs:
            s = kw.arg
            if kw.annotation:
                s += ": ..."
            parts.append(s)
        if args.kwarg:
            parts.append(f"**{args.kwarg.arg}")

        return ", ".join(parts)


class ImportFollower:
    """Orchestrator: parse imports from changed files, follow 1 level, extract definitions.

    Usage:
        follower = ImportFollower(repo_root="/path/to/project")
        context = await follower.follow(changed_files, file_contents)
    """

    def __init__(self, repo_root: str, max_depth: int = 1) -> None:
        self.repo_root = repo_root
        self.max_depth = max_depth
        self.resolver = ImportResolver(repo_root)
        self._visited: set[str] = set()

    async def follow(
        self,
        changed_files: dict[str, str],
    ) -> dict[str, str]:
        """Follow imports from changed files and extract contract context.

        Args:
            changed_files: dict of {relative_path: file_content} for changed files

        Returns:
            dict of {relative_path: extracted_context} for imported files
        """
        import asyncio

        context: dict[str, str] = {}
        self._visited = set(changed_files.keys())

        for file_path, content in changed_files.items():
            if not file_path.endswith(".py"):
                continue

            imports = ImportParser.parse(content)
            if not imports:
                continue

            logger.debug(
                "[IMPORT-FOLLOW] %s: found %d imports to follow",
                file_path, len(imports),
            )

            for imp in imports:
                # Resolve to filesystem path
                abs_source = Path(self.repo_root) / file_path
                resolved = self.resolver.resolve(imp, str(abs_source))
                if not resolved:
                    continue

                # Convert to repo-relative path
                try:
                    rel_path = str(resolved.relative_to(self.repo_root))
                except ValueError:
                    continue

                # Skip if already visited (changed file or already extracted)
                if rel_path in self._visited:
                    continue
                self._visited.add(rel_path)

                # Read and extract
                try:
                    dep_source = await asyncio.to_thread(resolved.read_text, encoding="utf-8")
                except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
                    logger.debug("[IMPORT-FOLLOW] Could not read %s: %s", rel_path, exc)
                    continue

                extracted = DefinitionExtractor.extract(
                    dep_source, rel_path, imported_names=imp.names or None
                )

                # Only include if we actually found meaningful content
                if extracted and len(extracted.strip().splitlines()) > 2:
                    context[rel_path] = extracted
                    logger.info(
                        "[IMPORT-FOLLOW] Extracted %d chars of contract context from %s",
                        len(extracted), rel_path,
                    )

        if context:
            logger.info(
                "[IMPORT-FOLLOW] Total: %d import context files extracted",
                len(context),
            )
        return context
