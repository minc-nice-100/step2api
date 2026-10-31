"""The project claims Python >= 3.9: keep every source file parseable as 3.9 syntax.

Runtime API usage is reviewed by hand; this catches the cheaper mistake of introducing
syntax the declared minimum cannot read (f-strings are fine, `match`, `X | Y` at runtime,
parenthesised context managers, etc. are not).

The second test defends the other half of the promise: the runtime is standard-library
only, so a stray `import requests` in the gateway must fail here rather than at install
time on a user's machine.
"""

import ast
import importlib.util
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", ".testdata", ".probe", "example", "accounts", "build", "dist"}


def sources():
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if name.endswith(".py"):
                yield os.path.join(base, name)


def runtime_sources():
    """Files that ship inside the package (scripts/ may use optional third-party SDKs)."""
    package = os.path.join(ROOT, "step2api")
    for name in sorted(os.listdir(package)):
        if name.endswith(".py"):
            yield os.path.join(package, name)
    yield os.path.join(ROOT, "step2api.py")


def _is_stdlib(name: str) -> bool:
    if name == "step2api" or name in sys.builtin_module_names:
        return True
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:                      # 3.10+
        return name in names
    spec = importlib.util.find_spec(name)      # 3.9 fallback
    origin = getattr(spec, "origin", "") or ""
    return bool(origin) and "site-packages" not in origin and "dist-packages" not in origin


class Python39SyntaxTest(unittest.TestCase):
    def test_all_sources_parse_as_python_39(self):
        problems = []
        for path in sorted(sources()):
            with io.open(path, encoding="utf-8") as fh:
                src = fh.read()
            try:
                ast.parse(src, filename=path, feature_version=(3, 9))
            except SyntaxError as exc:
                problems.append("%s:%s %s" % (os.path.relpath(path, ROOT), exc.lineno, exc.msg))
        self.assertEqual(problems, [])


class StdlibOnlyTest(unittest.TestCase):
    def test_runtime_imports_are_stdlib_or_sibling_only(self):
        problems = []
        for path in runtime_sources():
            with io.open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:               # relative import inside the package
                        continue
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    if name and not _is_stdlib(name):
                        problems.append("%s:%s imports third-party module %r"
                                        % (os.path.relpath(path, ROOT), node.lineno, name))
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
