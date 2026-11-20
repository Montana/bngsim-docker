"""Try bngsim: simulate the example models and check them against exact answers.

    docker run --rm montanamendy/bngsim python /examples/run.py

Copy the examples to start your own (your folder mounted at /work):

    docker run --rm -v "$PWD":/work montanamendy/bngsim cp -r /examples /work/
"""

import math
from pathlib import Path

import bngsim

HERE = Path(__file__).resolve().parent
print(f"bngsim {bngsim.__version__}\n")

# 1. A rule-based BNGL model, through BioNetGen (BNG2.pl), integrated as ODEs.
model = bngsim.Model.load(str(HERE / "receptor.bngl"))
ode = bngsim.Simulator(model, method="ode").run(t_span=(0, 50), n_points=6)
print("receptor.bngl (L + R <-> L.R), ODE:")
print("   time   bound")
for t, c in zip(ode.time, ode.observables["Bound"]):
    print(f"  {t:5.0f}  {c:6.1f}")
L0, R0, kon, koff = 1000, 500, 0.001, 0.1
b = kon * (L0 + R0) + koff
exact = (b - math.sqrt(b * b - 4 * kon * kon * L0 * R0)) / (2 * kon)
print(f"  equilibrium, exact: {exact:.1f}   simulated: {ode.observables['Bound'][-1]:.1f}\n")

# 2. The same model as a stochastic simulation (Gillespie SSA): whole molecules,
#    a different path each seed, settling around the same equilibrium.
for seed in (1, 2):
    ssa = bngsim.Simulator(bngsim.Model.load(str(HERE / "receptor.bngl")), method="ssa").run(
        t_span=(0, 50), n_points=6, seed=seed)
    print(f"receptor.bngl, SSA seed {seed}: bound at t = 50 is {ssa.observables['Bound'][-1]:.0f}")

# 3. An SBML model: A decays at rate k, so A(t) = 100 exp(-k t) exactly.
sbml = bngsim.Simulator(bngsim.Model.load(str(HERE / "decay.xml")), method="ode").run(
    t_span=(0, 4), n_points=5)
a = sbml.species[:, sbml.species_names.index("A")]
print("\ndecay.xml (SBML), ODE:")
for t, x in zip(sbml.time, a):
    print(f"  t = {t:.0f}   A = {x:8.4f}   exact {100 * math.exp(-0.5 * t):8.4f}")
