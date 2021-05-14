"""Large-eddy simulation

A simple psuedo-spectral LES for the TGV.
"""
import numpy

from psdns import *
from psdns.equations.navier_stokes import Smagorinsky


equations = Smagorinsky(Re=400)

solver = RungeKutta(
    dt=0.01,
    tfinal=10.0,
    equations=equations,
    ic=equations.taylor_green_vortex(
        SpectralGrid(sdims=2**5-1, pdims=3*2**4)
        ),
    diagnostics=[
        StandardDiagnostics(tdump=0.1, outfile="tgv.dat"),
        Spectra(tdump=1.0, outfile="spectra.dat"),
        ],
    )
solver.run()
solver.print_statistics()
