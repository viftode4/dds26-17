"""Generate stress test benchmark charts -- before/after optimization."""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

# ---- Data ------------------------------------------------------------------
runs = [
    {"label": "100u Before",  "throughput": 68.9,  "p50": 330, "p75": 460, "p90": 490, "p95": 500, "p99": 580,  "era": "before", "users": 100},
    {"label": "200u Before",  "throughput": 133.1, "p50": 420, "p75": 480, "p90": 580, "p95": 700, "p99": 1000, "era": "before", "users": 200},
    {"label": "100u After",   "throughput": 78.4,  "p50": 52,  "p75": 73,  "p90": 130, "p95": 230, "p99": 5500, "era": "after",  "users": 100},
    {"label": "200u After",   "throughput": 172.1, "p50": 46,  "p75": 78,  "p90": 170, "p95": 260, "p99": 420,  "era": "after",  "users": 200},
]

COLORS = {
    "100u Before": "#4C6FA5",
    "200u Before": "#7BA3D0",
    "100u After":  "#E85C4C",
    "200u After":  "#F4A05A",
}
PERCENTILES = ["p50", "p75", "p90", "p95"]
BG      = "#0F1117"
PANEL   = "#1A1D27"
GRID    = "#2A2D3A"

# ---- Figure ----------------------------------------------------------------
fig, axes = plt.subplots(1, 2, figsize=(16, 7))
fig.patch.set_facecolor(BG)
for ax in axes:
    ax.set_facecolor(PANEL)
    ax.tick_params(colors="white", labelsize=11)
    ax.spines[:].set_color(GRID)
    ax.yaxis.label.set_color("white")
    ax.xaxis.label.set_color("white")
    ax.title.set_color("white")

# ---- Plot 1: latency percentiles -------------------------------------------
ax1 = axes[0]
x    = np.arange(len(PERCENTILES))
n    = len(runs)
w    = 0.16
offs = np.linspace(-(n-1)*w/2, (n-1)*w/2, n)

for i, run in enumerate(runs):
    vals = [run[p] for p in PERCENTILES]
    capped = [min(v, 800) for v in vals]   # cap display at 800ms
    bars = ax1.bar(x + offs[i], capped, w,
                   label=run["label"],
                   color=COLORS[run["label"]], alpha=0.92, zorder=3)
    for bar, v in zip(bars, vals):
        label_v = f"{v}" if v < 1000 else f"{v//1000}s"
        ax1.text(bar.get_x() + bar.get_width()/2, min(v, 800) + 7,
                 label_v, ha="center", va="bottom",
                 fontsize=7.5, color="white", fontweight="bold")

ax1.set_xticks(x)
ax1.set_xticklabels(PERCENTILES, color="white", fontsize=13)
ax1.set_ylabel("Latency (ms)", color="white", fontsize=12)
ax1.set_title("Checkout Latency: Before vs After", color="white", fontsize=13, pad=14)
ax1.set_ylim(0, 950)
ax1.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
ax1.legend(facecolor=PANEL, edgecolor=GRID, labelcolor="white", fontsize=9,
           ncol=2, loc="upper left")

# ---- Plot 2: throughput & p50 ----------------------------------------------
ax2 = axes[1]
xlabels = ["100u\nBefore", "100u\nAfter", "200u\nBefore", "200u\nAfter"]
tps  = [r["throughput"] for r in runs]
p50s = [r["p50"] for r in runs]
cols = [COLORS[r["label"]] for r in runs]

bars = ax2.bar(xlabels, tps, color=cols, alpha=0.92, width=0.52, zorder=3)
for bar, tp, p50 in zip(bars, tps, p50s):
    ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1.5,
             f"{tp:.0f} req/s", ha="center", va="bottom",
             color="white", fontsize=10.5, fontweight="bold")
    ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height()/2,
             f"p50\n{p50}ms", ha="center", va="center",
             color="white", fontsize=9.5, fontweight="bold")

# improvement callout arrows
for before_i, after_i in [(0, 2), (1, 3)]:
    tp_imp = (tps[after_i] / tps[before_i] - 1) * 100
    p50_imp = p50s[before_i] / p50s[after_i]
    mid_x = (before_i + after_i) / 2
    ax2.annotate(
        f"+{tp_imp:.0f}% throughput\n{p50s[before_i]}ms -> {p50s[after_i]}ms p50\n({p50_imp:.1f}x faster)",
        xy=(mid_x, max(tps[before_i], tps[after_i]) + 18),
        fontsize=8.5, color="#FFD700", ha="center",
        bbox=dict(boxstyle="round,pad=0.35", facecolor=PANEL, edgecolor="#FFD700", alpha=0.95),
    )

ax2.set_ylabel("Requests / second", color="white", fontsize=12)
ax2.set_title("Throughput & p50 Latency", color="white", fontsize=13, pad=14)
ax2.set_ylim(0, 240)
ax2.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)

# ---- Footer ----------------------------------------------------------------
note = (
    "Optimization: parallel WAITAOF (10ms) + parallel 2PC/Saga command dispatch  |  "
    "0 real errors (all \"failures\" = correct idempotency: Order already paid)  |  "
    "Stack: 2 order + 2 stock + 2 payment + 3 Redis masters + 3 replicas + 3 Sentinels"
)
fig.text(0.5, -0.01, note, ha="center", fontsize=8.5, color="#9BA3C0", style="italic")

plt.suptitle("Distributed Checkout System -- Performance Optimization Results",
             color="white", fontsize=15, fontweight="bold", y=1.02)
plt.tight_layout(rect=[0, 0.03, 1, 1])

out = "docs/stress_test_results.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"Saved {out}")
