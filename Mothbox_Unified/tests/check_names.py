#!/usr/bin/python3
"""
check_names.py -- catch "used but never imported" before it reaches a box.

py_compile only proves a file PARSES. It happily accepts `shutil.which(...)`
with no `import shutil`, which then explodes at runtime -- in Sept 2026 exactly
that killed Scheduler.py at the camera check, before the boot lock was
released, leaving boxes dark with no blink and no display.

This walks every name the code LOADS and every name it BINDS anywhere in the
file (imports, assignments, defs, args, loop/with/except/comprehension
targets). Anything loaded but never bound and not a builtin is reported.
Binding is deliberately over-approximated across scopes, so there are no false
positives from local variables -- it only catches the genuinely-missing case.

    python3 tests/check_names.py            # every script in the firmware
    python3 tests/check_names.py Scheduler.py
"""
import ast, builtins, sys, os, glob as _glob

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "WindowsError"}

class Collector(ast.NodeVisitor):
    def __init__(self):
        self.bound, self.loaded = set(), {}
        self.star_import = False
    def bind(self, node):
        if isinstance(node, ast.Name): self.bound.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for e in node.elts: self.bind(e)
        elif isinstance(node, ast.Starred): self.bind(node.value)
    def visit_Import(self, n):
        for a in n.names: self.bound.add((a.asname or a.name).split(".")[0])
    def visit_ImportFrom(self, n):
        for a in n.names:
            if a.name == "*":
                # `from gps import *` -- we cannot know what it brings in, so
                # stop reporting anything in this file rather than cry wolf.
                self.star_import = True
            else:
                self.bound.add(a.asname or a.name)
    def visit_FunctionDef(self, n):
        self.bound.add(n.name)
        a = n.args
        for arg in list(a.args)+list(a.posonlyargs)+list(a.kwonlyargs)+([a.vararg] if a.vararg else [])+([a.kwarg] if a.kwarg else []):
            self.bound.add(arg.arg)
        self.generic_visit(n)
    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_ClassDef(self, n):
        self.bound.add(n.name); self.generic_visit(n)
    def visit_Assign(self, n):
        for t in n.targets: self.bind(t)
        self.generic_visit(n)
    def visit_AnnAssign(self, n):
        self.bind(n.target); self.generic_visit(n)
    def visit_AugAssign(self, n):
        self.bind(n.target); self.generic_visit(n)
    def visit_For(self, n):
        self.bind(n.target); self.generic_visit(n)
    visit_AsyncFor = visit_For
    def visit_comprehension(self, n):
        self.bind(n.target); self.generic_visit(n)
    def visit_withitem(self, n):
        if n.optional_vars is not None: self.bind(n.optional_vars)
        self.generic_visit(n)
    def visit_ExceptHandler(self, n):
        if n.name: self.bound.add(n.name)
        self.generic_visit(n)
    def visit_Global(self, n): self.bound.update(n.names)
    def visit_Nonlocal(self, n): self.bound.update(n.names)
    def visit_Name(self, n):
        if isinstance(n.ctx, ast.Load): self.loaded.setdefault(n.id, n.lineno)
        else: self.bound.add(n.id)

def check(path):
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read(), path)
    except SyntaxError as e:
        return [(getattr(e, "lineno", 0), f"SYNTAX ERROR: {e.msg}")]
    c = Collector(); c.visit(tree)
    if c.star_import:
        return []
    return sorted((ln, f"name '{name}' is used but never imported or assigned")
                  for name, ln in c.loaded.items() if name not in c.bound and name not in BUILTINS)

if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    targets = sys.argv[1:] or [p for p in _glob.glob(os.path.join(root, "*.py"))
                               + _glob.glob(os.path.join(root, "scripts", "*.py"))
                               if "legacy" not in p and "OldCustom" not in p]
    bad = 0
    for t in sorted(targets):
        for ln, msg in check(t):
            print(f"{os.path.relpath(t, root)}:{ln}: {msg}"); bad += 1
    print(f"\n{bad} problem(s) found" if bad else "\nNo undefined names in any script.")
    sys.exit(1 if bad else 0)
