"""Fine comparisons only at the promising medium target and segment boundary."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from circulator_gauntlet import run,configuration
for name,conf in [('fixed-28000',configuration(28000)),('fixed-32000',configuration(32000)),('small-segments-28000',configuration(28000,6000))]:
    run(name,conf,repeat=3)
