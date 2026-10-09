#!/usr/bin/python3
"""
The shutdown deadman must outlast everything the shutdown path can legitimately
wait for. If it does not, the safety net becomes the fault: it force-halts a box
that was still finishing a slow backup, mid-write, which is exactly the
corruption it exists to prevent.

    python3 tests/test_shutdown_budget.py
"""
import ast, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tree = ast.parse(open(os.path.join(ROOT, "Scheduler.py")).read())

def const_value(node):
    """literal_eval, but also folds simple arithmetic like `15 * 60`."""
    try:
        return ast.literal_eval(node)
    except Exception:
        pass
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add)):
        a, b = const_value(node.left), const_value(node.right)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a * b if isinstance(node.op, ast.Mult) else a + b
    return None

consts = {}
for n in tree.body:
    if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
        v = const_value(n.value)
        if v is not None: consts[n.targets[0].id] = v
defaults = {}
for n in ast.walk(tree):
    if isinstance(n, ast.FunctionDef):
        pairs = list(zip(n.args.args[::-1], n.args.defaults[::-1]))
        pairs += [(a, d) for a, d in zip(n.args.kwonlyargs, n.args.kw_defaults) if d is not None]
        for a, d in pairs:
            if a.arg == "timeout_s":
                v = const_value(d)
                if v is not None: defaults[n.name] = v

parts = {
    "wait for in-flight TakePhoto": consts["TAKEPHOTO_DRAIN_TIMEOUT_S"],
    "final backup":                 consts["FINAL_BACKUP_TIMEOUT_S"],
    "UpdateDisplay (run_helper)":   defaults["run_helper"],
    "Diagnostics (run_script)":     defaults["run_script"],
    "fixed sleeps":                 5,
}
worst = sum(parts.values())
dead  = consts["SHUTDOWN_DEADMAN_S"]
for k, v in parts.items():
    print(f"  {k:32s} {v:5d}s")
print(f"  {'-'*40}")
print(f"  {'worst-case shutdown':32s} {worst:5d}s ({worst/60:.1f} min)")
print(f"  {'SHUTDOWN_DEADMAN_S':32s} {dead:5d}s ({dead/60:.1f} min)")
margin = dead - worst
print(f"  {'margin':32s} {margin:+5d}s")
if margin <= 0:
    print("\nFAIL: the deadman would force-halt a shutdown that was still working.")
    sys.exit(1)
print("\nOK: the deadman only fires after everything legitimate has already given up.")
