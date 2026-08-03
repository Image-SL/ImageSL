import sys,glob
sys.path.insert(0,r"C:/Users/renyi/Downloads/ImageSL/ImageSL/server")
sys.path.insert(0,r"C:/Users/renyi/Downloads/ImageSL/ImageSL/scripts")
from ihc import detect
detect.EXTENT_PEAK_FRAC=0.26; detect.EXTENT_PEAK_FRAC_FINE=0.26; detect.SMOOTH_SIGMA=1.0
import backtest as BT
sys.argv=["backtest", r"C:/Users/renyi/AppData/Local/Temp/claude/C--Users-renyi-Downloads-SolanaZ/005d7cb7-050e-4195-92a1-1eb2b8db4cad/scratchpad/slides"]
raise SystemExit(BT.main())
