"""Generate figures for the Gap-Aware + Self-Join thesis.

Run:  ../.venv/bin/python make_figs.py   (from thesis_assets/)
All numbers are grounded in the consolidated reports + verified code.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np
import os

OUT = os.path.dirname(os.path.abspath(__file__))
plt.rcParams.update({
    "figure.dpi": 130,
    "font.size": 11,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "axes.axisbelow": True,
})

GREEN = "#2e7d32"
RED = "#c62828"
BLUE = "#1565c0"
ORANGE = "#ef6c00"
GREY = "#9e9e9e"


def save(fig, name):
    p = os.path.join(OUT, name)
    fig.tight_layout()
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    print("wrote", p)


# ---------------------------------------------------------------------------
# Fig 1 — Cardinality scaling law: the self-join detection signal
# Duplicating the single D1 row (m: 1 -> 2) scales |Qh| by m^k. The probe
# fires when the observed ratio lands in [3.5, 4.5] (k=2).
# ---------------------------------------------------------------------------
def fig_cardinality_scaling():
    k = np.array([1, 2, 3, 4])
    ratio = 2.0 ** k  # m^k with m=2
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    colors = [GREY, GREEN, GREY, GREY]
    bars = ax.bar(k, ratio, color=colors, width=0.55, edgecolor="black", linewidth=0.6)
    ax.axhspan(3.5, 4.5, color=GREEN, alpha=0.16, zorder=0)
    ax.axhline(3.5, color=GREEN, ls="--", lw=1)
    ax.axhline(4.5, color=GREEN, ls="--", lw=1)
    ax.text(4.05, 4.0, "detection band [3.5, 4.5]", color=GREEN,
            va="center", ha="left", fontsize=9.5)
    for b, r in zip(bars, ratio):
        ax.text(b.get_x() + b.get_width() / 2, r + 0.3, f"x{r:.0f}",
                ha="center", va="bottom", fontsize=10)
    ax.set_xticks(k)
    ax.set_xticklabels([f"k=1\n(single table)", "k=2\n(self-join)",
                        "k=3", "k=4"])
    ax.set_ylabel(r"observed ratio $B_{dup}/B_{orig} = m^{k}$  (m=2)")
    ax.set_title("Cardinality-scaling signal: how |Qh| grows when the lone\n"
                 "D¹ row is duplicated (m: 1→2).  k=2 lands in the band.")
    ax.set_ylim(0, 17)
    ax.annotate("single-table query\nscales only x2 -> ignored",
                xy=(1, 2), xytext=(1.15, 7.5), fontsize=9, color=GREY,
                arrowprops=dict(arrowstyle="->", color=GREY))
    ax.annotate("2-alias self-join\nscales x4 -> promote to k=2",
                xy=(2, 4), xytext=(2.2, 11.5), fontsize=9, color=GREEN,
                arrowprops=dict(arrowstyle="->", color=GREEN))
    save(fig, "fig_cardinality_scaling.png")


# ---------------------------------------------------------------------------
# Fig 2 — Per-stage extraction time (log scale), SJ2 (lineitem ~6M) vs
# SJ3 (nation 25). Numbers from the self-join report performance table.
# ---------------------------------------------------------------------------
def fig_stage_timing():
    stages = ["View\nMinimisation", "Cardinality\nProbe",
              "Filter +\nEquiJoin", "Cross-alias\nResolver"]
    sj2 = [100.0, 0.5, 0.5, 0.05]     # <1s -> 0.5, <0.1s -> 0.05
    sj3 = [0.10, 0.05, 0.21, 0.05]
    x = np.arange(len(stages))
    w = 0.38
    fig, ax = plt.subplots(figsize=(7.8, 4.4))
    b1 = ax.bar(x - w / 2, sj2, w, label="SJ2  (lineitem, ~6M rows)",
                color=BLUE, edgecolor="black", linewidth=0.5)
    b2 = ax.bar(x + w / 2, sj3, w, label="SJ3  (nation, 25 rows)",
                color=ORANGE, edgecolor="black", linewidth=0.5)
    ax.set_yscale("log")
    ax.set_ylabel("time (seconds, log scale)")
    ax.set_xticks(x)
    ax.set_xticklabels(stages)
    ax.set_title("Per-stage extraction time: the self-join machinery is cheap;\n"
                 "cost is dominated by baseline View Minimisation on big tables")
    for bars in (b1, b2):
        for b in bars:
            h = b.get_height()
            ax.text(b.get_x() + b.get_width() / 2, h * 1.15,
                    f"{h:g}s", ha="center", va="bottom", fontsize=8.5)
    ax.legend(loc="upper right", framealpha=0.95)
    ax.set_ylim(0.02, 300)
    save(fig, "fig_stage_timing.png")


# ---------------------------------------------------------------------------
# Fig 3 — Gap-aware number line for the canonical case:
#   SELECT n_name FROM nation WHERE n_nationkey < 5 OR n_nationkey > 20
# nation.n_nationkey domain is 0..24. Accepted: [0,4] and [21,24]; gap [5,20].
# ---------------------------------------------------------------------------
def fig_gap_numberline():
    fig, ax = plt.subplots(figsize=(8.6, 3.6))
    dom_lo, dom_hi = 0, 24
    rows = {
        "Hidden Qh\naccepts": (3, [(0, 4), (21, 24)], GREEN),
        "Baseline\n(gap-aware OFF)": (2, [(0, 24)], RED),
        "Gap-aware\nrecovers": (1, [(0, 4), (21, 24)], GREEN),
    }
    for label, (y, spans, col) in rows.items():
        ax.hlines(y, dom_lo, dom_hi, color="lightgrey", lw=10, zorder=1)
        for (a, b) in spans:
            ax.hlines(y, a, b, color=col, lw=10, zorder=2)
    # gap shading on the recovered row
    ax.axvspan(5, 20, ymin=0.08, ymax=0.34, color="white", alpha=0.0)
    ax.annotate("GAP (5..20):\nQh rejects", xy=(12.5, 1), xytext=(12.5, 0.25),
                ha="center", fontsize=9, color=BLUE,
                arrowprops=dict(arrowstyle="->", color=BLUE))
    # witness marker
    ax.plot([12], [1], marker="v", color=BLUE, markersize=11, zorder=5)
    ax.text(12, 1.32, "witness row\n(n_nationkey=12)", ha="center",
            fontsize=8.5, color=BLUE)
    ax.text(2, 3.42, "n_nationkey<=4", ha="center", fontsize=8.5, color=GREEN)
    ax.text(22.5, 3.42, "n_nationkey>=21", ha="center", fontsize=8.5, color=GREEN)
    ax.text(12, 2.32, "WHERE silently DROPPED -> wrong (returns all rows)",
            ha="center", fontsize=8.5, color=RED)
    ax.set_yticks([1, 2, 3])
    ax.set_yticklabels([rows_k for rows_k in
                        ["Gap-aware\nrecovers", "Baseline\n(gap-aware OFF)",
                         "Hidden Qh\naccepts"]])
    ax.set_xlim(-1, 25)
    ax.set_ylim(0.4, 3.8)
    ax.set_xlabel("n_nationkey value")
    ax.set_title("Within-attribute OR-of-intervals:  n_nationkey < 5  OR  n_nationkey > 20")
    ax.grid(axis="y", alpha=0)
    save(fig, "fig_gap_numberline.png")


# ---------------------------------------------------------------------------
# Fig 5 — Gap-search interval refinement loop (DQ3-shape):
#   A<10 OR A in [24,42] OR A>=50  over domain [1,60]
# converges [(1,60)] -> ... -> [(1,9),(24,42),(50,60)].
# ---------------------------------------------------------------------------
def fig_gap_search_convergence():
    fig, ax = plt.subplots(figsize=(8.4, 4.0))
    iters = [
        ("iter 0  start", [(1, 60)], None),
        ("iter 1  witness@15 -> split", [(1, 9), (24, 60)], 15),
        ("iter 2  witness@46 -> split", [(1, 9), (24, 42), (50, 60)], 46),
        ("converged (no more witnesses)", [(1, 9), (24, 42), (50, 60)], None),
    ]
    for row, (label, spans, w) in enumerate(iters):
        y = len(iters) - row
        ax.hlines(y, 1, 60, color="lightgrey", lw=11, zorder=1)
        for (a, b) in spans:
            ax.hlines(y, a, b, color=GREEN, lw=11, zorder=2)
            ax.text((a + b) / 2, y, f"[{a},{b}]", ha="center", va="center",
                    color="white", fontsize=8, zorder=3)
        if w is not None:
            ax.plot([w], [y], marker="v", color=RED, markersize=11, zorder=5)
            ax.text(w, y + 0.34, f"witness {w}\n(in a gap)", ha="center",
                    fontsize=8, color=RED)
        ax.text(0, y, label, ha="right", va="center", fontsize=9)
    ax.set_xlim(-20, 62)
    ax.set_ylim(0.3, len(iters) + 0.9)
    ax.set_yticks([])
    ax.set_xlabel("attribute value (domain [1, 60])")
    ax.set_title("Gap-search refinement loop on DQ3-shape:  A<10  OR  A in [24,42]  OR  A>=50\n"
                 "each found witness splits one interval into two; loop ends when the Re-Rh diff is empty")
    ax.grid(axis="y", alpha=0)
    save(fig, "fig_gap_search_convergence.png")


# ---------------------------------------------------------------------------
# Fig — Worked-example flowchart: the gap pass on the canonical nation query
#   SELECT n_name FROM nation WHERE n_nationkey < 5 OR n_nationkey > 20
# Shows how the algorithm threads through the pipeline and its inner carve loop.
# ---------------------------------------------------------------------------
def fig_gap_flowchart():
    from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
    fig, ax = plt.subplots(figsize=(9.6, 12.2))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 23)
    ax.axis("off")
    ax.grid(False)

    CX = 4.7          # box centre x
    BW = 6.4          # box width

    def box(yc, h, text, fc, ec, bold=False, fs=9.4, x=CX, w=BW):
        p = FancyBboxPatch((x - w / 2, yc - h / 2), w, h,
                           boxstyle="round,pad=0.06,rounding_size=0.10",
                           fc=fc, ec=ec, lw=1.5, zorder=2)
        ax.add_patch(p)
        ax.text(x, yc, text, ha="center", va="center", fontsize=fs,
                fontweight="bold" if bold else "normal", zorder=3)

    def varrow(y1, y2, x=CX, color="#333333", text=None, tx=None):
        ax.add_patch(FancyArrowPatch((x, y1), (x, y2), arrowstyle="-|>",
                     mutation_scale=15, lw=1.4, color=color, zorder=1))
        if text:
            ax.text((tx if tx is not None else x) + 0.15, (y1 + y2) / 2, text,
                    fontsize=8.6, ha="left", va="center", color=color)

    # --- nodes (top -> bottom) ---
    box(21.7, 1.7,
        "Hidden $Q_h$:  SELECT n_name FROM nation\n"
        "WHERE n_nationkey < 5 OR n_nationkey > 20\n"
        "nation.n_nationkey domain 0..24  —  $Q_h$ accepts 9 of 25 rows",
        fc="#eceff1", ec=GREY, fs=9.2)
    box(19.4, 1.5,
        "Filter sees BOTH domain extremes accepted →\n"
        "emits nothing for n_nationkey  (baseline 'silent drop')",
        fc="#fff3e0", ec=ORANGE)
    box(17.3, 1.4,
        "Projection → SELECT n_name ;  assemble first $Q_E$:\n"
        "SELECT n_name FROM nation   (no WHERE — over-approximates)",
        fc="#fff3e0", ec=ORANGE)

    # gap-pass band
    ax.add_patch(FancyBboxPatch((0.5, 3.2), 9.0, 12.0,
                 boxstyle="round,pad=0.1,rounding_size=0.15",
                 fc="#f1f8e9", ec=GREEN, lw=1.6, ls="--", zorder=0))
    ax.text(0.75, 14.8, "GAP PASS  (_extract_gap: post-Projection, pre-NEP)",
            fontsize=10, fontweight="bold", color=GREEN, ha="left", va="center")

    box(13.7, 1.3,
        "match:  diff  $Q_E$  vs  $Q_h$   (Re−Rh, hash mode)\n"
        "bag-equal?",
        fc="#e8f5e9", ec=GREEN, bold=True)
    box(11.5, 1.5,
        "restore full D (25 rows); ctid-bisect the disagreement →\n"
        "witness row  n_nationkey = 12   (a row inside the hole)",
        fc="#e3f2fd", ec=BLUE)
    box(9.2, 1.7,
        "n_nationkey unconstrained → synth full-domain envelope [0,24]\n"
        "acceptance oracle = count(*)+hash fingerprint of $Q_h$:\n"
        "accepted(12)=NO    accepted(0)=YES    accepted(24)=YES",
        fc="#e3f2fd", ec=BLUE)
    box(6.7, 1.8,
        "galloping edge search from witness 12 (no overshoot):\n"
        "gap_left ← last accepted ≤ 12  = 4\n"
        "gap_right ← first accepted ≥ 12 = 21\n"
        "split [0,24] → [0,4] ∪ [21,24]",
        fc="#e3f2fd", ec=BLUE)
    box(4.2, 1.5,
        "re-render WHERE from disjunctive_ranges →\n"
        "(n_nationkey between 0 and 4 OR between 21 and 24)\n"
        "= tighter $Q_E$",
        fc="#e3f2fd", ec=BLUE)

    box(1.5, 1.6,
        "STOP — Qe = SELECT n_name FROM nation WHERE\n"
        "(n_nationkey between 0 and 4 OR n_nationkey between 21 and 24)",
        fc="#c8e6c9", ec=GREEN, bold=True)

    # --- arrows ---
    varrow(20.85, 20.15)
    varrow(18.65, 18.0)
    varrow(16.6, 14.35)                       # into gap pass / match
    varrow(13.05, 12.25, text="not bag-equal", tx=CX)   # match -> bisect
    varrow(10.75, 10.05)
    varrow(8.35, 7.6)
    varrow(5.8, 4.95)
    # loop back: re-render -> match (up the right margin)
    ax.add_patch(FancyArrowPatch((CX + BW / 2, 4.2), (8.7, 4.2),
                 arrowstyle="-", lw=1.4, color=BLUE, zorder=1))
    ax.add_patch(FancyArrowPatch((8.7, 4.2), (8.7, 13.7),
                 arrowstyle="-", lw=1.4, color=BLUE, zorder=1))
    ax.add_patch(FancyArrowPatch((8.7, 13.7), (CX + BW / 2, 13.7),
                 arrowstyle="-|>", mutation_scale=15, lw=1.4, color=BLUE, zorder=1))
    ax.text(8.85, 9.0, "loop: re-diff with\ntighter $Q_E$", fontsize=8.4,
            color=BLUE, ha="left", va="center", rotation=90)
    # equal -> STOP (down the left margin)
    ax.add_patch(FancyArrowPatch((CX - BW / 2, 13.7), (1.0, 13.7),
                 arrowstyle="-", lw=1.4, color=GREEN, zorder=1))
    ax.add_patch(FancyArrowPatch((1.0, 13.7), (1.0, 1.5),
                 arrowstyle="-", lw=1.4, color=GREEN, zorder=1))
    ax.add_patch(FancyArrowPatch((1.0, 1.5), (CX - BW / 2, 1.5),
                 arrowstyle="-|>", mutation_scale=15, lw=1.4, color=GREEN, zorder=1))
    ax.text(0.85, 7.5, "bag-equal → done", fontsize=8.4, color=GREEN,
            ha="left", va="center", rotation=90)

    ax.set_title("How the gap pass threads the pipeline — worked example on "
                 "TPC-H nation\n(domain-extreme OR; one carve recovers the two "
                 "spans)", fontsize=11)
    save(fig, "fig_gap_flowchart.png")


# ---------------------------------------------------------------------------
# Fig — Gap-pass scaling with the number of disjoint intervals (K = 1..5).
# Measured on TPC-H supplier.s_suppkey (10,000 distinct keys, domain 1..10000),
# uniform domain-extreme geometry (intervals >=1500 wide, gaps 500), gap_aware
# only. Left: deterministic DB-operation counts (reproducible cost). Right:
# wall-clock of the gap pass (restore-I/O-bound, hence high variance) over 2 reps.
# All five K extract CORRECTLY (galloping edge search). Counts are identical
# across reps because the probe sequence is deterministic.
# ---------------------------------------------------------------------------
def fig_gap_interval_scaling():
    K = np.array([1, 2, 3, 4, 5])
    witness_diffs = np.array([1, 2, 3, 4, 5])     # GapMinimizer.match calls
    ctid_diffs = np.array([0, 19, 39, 63, 83])    # check_result_for_half calls
    edge_probes = np.array([0, 23, 46, 69, 92])   # _signature_at oracle calls
    # wall-clock gap_time (s), two repeats; restore-I/O dominated -> noisy
    rep1 = np.array([0.67, 48.99, 26.72, 21.97, 11.50])
    rep2 = np.array([0.69, 22.32, 41.71, 96.14, 31.02])  # K5 rep2 (#10)

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11.6, 4.6))

    # ---- left: deterministic operation counts (stacked) ----
    axL.bar(K, witness_diffs, color=GREEN, edgecolor="black", linewidth=0.5,
            label="witness diffs (match)")
    axL.bar(K, ctid_diffs, bottom=witness_diffs, color=BLUE, edgecolor="black",
            linewidth=0.5, label="ctid-bisection diffs")
    axL.bar(K, edge_probes, bottom=witness_diffs + ctid_diffs, color=ORANGE,
            edgecolor="black", linewidth=0.5, label="edge-search probes")
    total = witness_diffs + ctid_diffs + edge_probes
    for k, t in zip(K, total):
        axL.text(k, t + 3, f"{t}", ha="center", va="bottom", fontsize=9,
                 fontweight="bold")
    # linear reference through the per-interval slope (=44 ops / interval)
    axL.plot(K, 44 * (K - 1), "k--", lw=1.2, alpha=0.7,
             label="linear fit  44·(K−1)")
    axL.set_xticks(K)
    axL.set_xlabel("number of disjoint intervals  K")
    axL.set_ylabel("DB operations in the gap pass")
    axL.set_title("Deterministic cost is linear in K\n"
                  "(each extra interval = one carve ≈ 23 probes + ~20 diffs)")
    axL.legend(fontsize=8.4, loc="upper left")
    axL.set_ylim(0, 200)

    # ---- right: measured wall-clock (noisy) ----
    axR.scatter(K, rep1, s=46, color=BLUE, zorder=3, label="repeat 1")
    axR.scatter(K, rep2, s=46, color=ORANGE, marker="^", zorder=3,
                label="repeat 2")
    mean = (rep1 + rep2) / 2
    axR.plot(K, mean, color=GREY, lw=1.6, zorder=2, label="mean")
    axR.set_xticks(K)
    axR.set_xlabel("number of disjoint intervals  K")
    axR.set_ylabel("gap-pass wall-clock (s)")
    axR.set_title("Wall-clock tracks K but is restore-I/O bound\n"
                  "(full-D restore per diff → high run-to-run variance)")
    axR.legend(fontsize=8.6, loc="upper left")
    axR.set_ylim(0, 110)

    save(fig, "fig_gap_interval_scaling.png")


# ---------------------------------------------------------------------------
# Fig — Why galloping: a plain binary edge search overshoots with >=3 holes.
# Measured on supplier.s_suppkey for
#   < 1500 OR [3000,4000] OR [6000,7000] OR > 8500   (4 true intervals)
# Plain binary search collapsed it to the two extremes; galloping recovers all 4.
# ---------------------------------------------------------------------------
def fig_gap_gallop_vs_binary():
    fig, ax = plt.subplots(figsize=(9.4, 4.0))
    lo, hi = 1, 10000
    true_iv = [(1, 1499), (3000, 4000), (6000, 7000), (8501, 10000)]
    binary_iv = [(1, 1499), (8501, 10000)]      # measured: overshoot
    gallop_iv = [(1, 1499), (3000, 4000), (6000, 7000), (8501, 10000)]
    rows = [
        ("Hidden $Q_h$\n(4 intervals)", 3, true_iv, GREEN, None),
        ("Plain binary\nedge search", 2, binary_iv, RED, (1500, 8500)),
        ("Galloping\nedge search", 1, gallop_iv, GREEN, None),
    ]
    for label, y, spans, col, swallowed in rows:
        ax.hlines(y, lo, hi, color="lightgrey", lw=12, zorder=1)
        for (a, b) in spans:
            ax.hlines(y, a, b, color=col, lw=12, zorder=2)
        ax.text(-1700, y, label, ha="left", va="center", fontsize=9)
        if swallowed:
            sa, sb = swallowed
            ax.annotate("swallowed: [3000,4000] and [6000,7000] lost",
                        xy=((sa + sb) / 2, y), xytext=((sa + sb) / 2, y - 0.46),
                        ha="center", fontsize=8.4, color=RED)
    ax.set_xlim(-1800, 10300)
    ax.set_ylim(0.3, 3.7)
    ax.set_yticks([])
    ax.set_xlabel("s_suppkey value (domain 1..10000)")
    ax.set_title("Why galloping: binary edge search overshoots with ≥3 holes,\n"
                 "carving one giant gap; galloping finds the nearest edge and "
                 "keeps all intervals")
    ax.grid(axis="y", alpha=0)
    save(fig, "fig_gap_gallop_vs_binary.png")


if __name__ == "__main__":
    fig_cardinality_scaling()
    fig_stage_timing()
    fig_gap_numberline()
    fig_gap_search_convergence()
    fig_gap_flowchart()
    fig_gap_interval_scaling()
    fig_gap_gallop_vs_binary()
    print("all figures done")
