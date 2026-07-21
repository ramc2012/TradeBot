"""VERIFY-D3: recompute the GLOBAL correction across the whole registered grid,
with the REPAIRED option cells substituted for the ones built on the truncated
tape, and count survivors independently of the study's own bookkeeping."""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DIV = os.path.abspath(os.path.join(HERE, ".."))
DATA = os.path.join(DIV, "data")
sys.path.insert(0, os.path.join(DIV, "..", "cascade"))
import run_cascade as rc

c = pd.read_csv(os.path.join(DATA, "combined_tests.csv"))
print("registered comparisons as shipped:", len(c),
      "\n by family:\n", c.groupby("family").size().to_string())

# --- swap the option family for the repaired one -------------------------
opt_new = pd.read_csv(os.path.join(DATA, "opt_tests2.csv"))
opt_new["label"] = opt_new["arm"] + " " + opt_new["band"] + " vs ctrl_random (repaired)"
opt_new["metric"] = "net_base"; opt_new["family"] = "option"
keep = c[c["family"] != "option"][["label", "metric", "n_a", "n_b", "mean_a", "mean_b",
                                   "diff", "p", "family"]]
new = pd.concat([keep, opt_new[["label", "metric", "n_a", "n_b", "mean_a", "mean_b",
                                "diff", "p", "family"]]], ignore_index=True)
print(f"\nrecombined grid: {len(new)} comparisons "
      f"({len(keep)} non-option + {len(opt_new)} repaired option)")

new["q_bh"] = rc.bh(list(new["p"]))
new["p_bonf"] = np.minimum(1.0, new["p"] * len(new))
new = new.sort_values("p")
print("\n--- everything with raw p < 0.05 ---")
print(new[new["p"] < 0.05][["label", "metric", "n_a", "n_b", "mean_a", "mean_b",
                            "diff", "p", "q_bh", "p_bonf", "family"]].to_string(
    index=False, float_format=lambda v: f"{v:.4f}"))
print(f"\nBonferroni p<0.05 survivors : {int((new['p_bonf'] < 0.05).sum())}")
print(f"BH q<0.10 survivors         : {int((new['q_bh'] < 0.10).sum())}")
print(f"BH q<0.05 survivors         : {int((new['q_bh'] < 0.05).sum())}")
new.to_csv(os.path.join(DATA, "combined_tests_repaired.csv"), index=False)
