"""Run inside the bngsim image: every format it promises loads and simulates.

A BNGL model goes through BNG2.pl; its ODE run is checked against the exact
solution A0 exp(-k t), and an SSA run against a fixed seed's repeatability. An
SBML model and an Antimony model are checked against the same solution, on
either architecture (arm64's antimony is built from source in the image). Exit
status 1 on the first that fails."""

from __future__ import annotations

import importlib.util
import math
import platform
import sys
from pathlib import Path

import bngsim

HERE = Path(__file__).resolve().parent


def species(result, name):
    """One species' column, by name."""
    return result.species[:, result.species_names.index(name)]


print("bngsim", bngsim.__version__, "on", platform.machine())

model = bngsim.Model.load(str(HERE / "decay.bngl"))
ode = bngsim.Simulator(model, method="ode").run(t_span=(0.0, 4.0), n_points=5)
a = ode.observables["Atot"]
want = [100 * math.exp(-0.5 * t) for t in (0, 1, 2, 3, 4)]
assert all(abs(x - y) <= 1e-4 * y for x, y in zip(a, want)), (list(a), want)
print("BNGL through BNG2.pl, ODE: matches A0 exp(-k t)")

# A model keeps its state from run to run (a continued run): each SSA run gets a
# fresh one, so both start from A0.
ssa1 = bngsim.Simulator(bngsim.Model.load(str(HERE / "decay.bngl")), method="ssa").run(
    t_span=(0.0, 4.0), n_points=5, seed=7)
ssa2 = bngsim.Simulator(bngsim.Model.load(str(HERE / "decay.bngl")), method="ssa").run(
    t_span=(0.0, 4.0), n_points=5, seed=7)
s1 = ssa1.observables["Atot"]
s2 = ssa2.observables["Atot"]
assert list(s1) == list(s2) and s1[0] == 100 and s1[-1] < 100, (list(s1), list(s2))
print("SSA: a seed repeats, and A decays")

sbml = bngsim.Simulator(bngsim.Model.load(str(HERE / "decay.xml")), method="ode").run(t_span=(0.0, 4.0), n_points=5)
b = species(sbml, "A")
assert all(abs(x - y) <= 1e-4 * y for x, y in zip(b, want)), (list(b), want)
print("SBML: matches A0 exp(-k t)")

if not importlib.util.find_spec("antimony"):
    sys.exit(f"antimony is missing on {platform.machine()}, where the image installs it")
ant = bngsim.Model.from_antimony_string("J0: A -> ; k*A; A = 100; k = 0.5")
c = species(bngsim.Simulator(ant, method="ode").run(t_span=(0.0, 4.0), n_points=5), "A")
assert all(abs(x - y) <= 1e-4 * y for x, y in zip(c, want)), (list(c), want)
print("Antimony: matches A0 exp(-k t)")
print("all checks passed")
