"""Every intra-package import resolves: `from .x import Name`
finds both x.py and Name at its top level.

Minted after two field startup breaks in one series (hig.py's
ratchet rule: a dispute settled by hand becomes a mechanical
rule) -- first a patch importing a module its commit did not
carry, then a name its module did not define. py_compile
compiles without importing, pyflakes stops at module borders,
and the sandbox has no gi to import the GUI modules for real;
AST is the check that CAN run here, no imports executed."""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "perdeviceeq"


def _scan(body, names):
    for n in body:
        if isinstance(n, (ast.ClassDef, ast.FunctionDef,
                          ast.AsyncFunctionDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    for e in t.elts:
                        if isinstance(e, ast.Name):
                            names.add(e.id)
        elif isinstance(n, ast.AnnAssign):
            if isinstance(n.target, ast.Name):
                names.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.Try):
            for part in (n.body, n.orelse, n.finalbody):
                _scan(part, names)
            for h in n.handlers:
                _scan(h.body, names)
        elif isinstance(n, ast.If):
            _scan(n.body, names)
            _scan(n.orelse, names)


def _top_names(tree):
    names = set()
    _scan(tree.body, names)
    return names


def test_intra_package_imports_resolve():
    trees = {p.stem: ast.parse(p.read_text(encoding="utf-8"))
             for p in PKG.glob("*.py")}
    tops = {m: _top_names(t) for m, t in trees.items()}
    bad = []
    for mod, tree in trees.items():
        for n in ast.walk(tree):
            if not isinstance(n, ast.ImportFrom) or n.level != 1:
                continue
            if n.module is None:         # from . import x, y
                for a in n.names:
                    if (a.name not in trees
                            and a.name not in tops["__init__"]):
                        bad.append("%s: from . import %s"
                                   % (mod, a.name))
                continue
            if n.module not in trees:
                bad.append("%s: from .%s import ..."
                           % (mod, n.module))
                continue
            for a in n.names:
                if a.name != "*" and a.name not in tops[n.module]:
                    bad.append("%s: from .%s import %s"
                               % (mod, n.module, a.name))
    assert not bad, bad
