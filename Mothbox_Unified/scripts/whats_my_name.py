#!/usr/bin/python3
"""
whats_my_name.py -- print the auto-generated Mothbox name this Pi gets under
the CURRENT naming (firmware >= 5.2.1 / 4.18.1, md5 seed) and under the OLD
naming (sum-of-bytes seed). Pulls the real functions out of the installed
Scheduler.py so it can never drift from what the box actually does.

  python3 /home/pi/Desktop/Mothbox/scripts/whats_my_name.py [serial]
"""
import ast, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCHED = os.path.join(os.path.dirname(HERE), "Scheduler.py")
WORDS = os.path.join(os.path.dirname(HERE), "wordlist.csv")

tree = ast.parse(open(SCHED).read())
want = {"read_csv_into_lists", "word_to_seed", "word_to_seed_old", "generate_unique_name", "get_serial_number"}
fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in want]
ns = {"np": np, "csv": __import__("csv"), "os": os}
exec(compile(ast.Module(fns, []), SCHED, "exec"), ns)

data = ns["read_csv_into_lists"](WORDS)
ns.update(animals=data["Animal2"], adjectives=data["Adjectives"], colors=data["Colors"], verbs=data["Verbs"],
          animales=data["Animales"], adjectivos=data["Adjectivos"], verbos=data["Verbos"],
          colores=data["Colores"], sustantivos=data["Sustantivos"])

serial = sys.argv[1] if len(sys.argv) > 1 else ns["get_serial_number"]()
print(f"serial: {serial}")
new = ns["generate_unique_name"](serial, 3)
ns["word_to_seed"] = ns["word_to_seed_old"]          # swap the seed the generator calls
old = ns["generate_unique_name"](serial, 3)
print(f"name with CURRENT hashing (>= 5.2.1): {new}")
print(f"name with OLD hashing     (<  5.2.1): {old}")
