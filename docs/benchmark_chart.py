"""Generate stress test benchmark charts."""
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ── Data ────────────────────────────────────────────────────────────────────
results = {
    "100 users\n(68.9 req/s)": {
        "throughput": 68.9,
        "p50": 330, "p66": 440, "p75": 460,
        "p80": 470, "p90": 490, "p95": 500, "p99": 580,
        "color": "#4C9BE8",
    },
    "200 users\n(133 req/s)": {
        "throughput": 133.1,
        "p50": 420, "p66": 450, "p75": 480,
        "p80": 500, "p90": 580, "p95": 700, "p99": 1000,
        "color": "#E85C4C",
    },
}

percentiles = ["p50", "p66", "p75", "p80", "p90", "p95", "p99"]
labels      = ["p50", "p66", "p75", "p80", "p90", "p95", "p99"]

# ── Figure layout ────────────────────────────────────────────────────────────
fig, axes = plt.subplots(1, 2, figsize=(14, 6))
fig.patch.set_facecolor("#0F1117")
for ax in axes:
    ax.set_facecolor("#1A1D27")
    ax.tick_params(colors="white")
    ax.spines[:].set_color("#2A2D3A")
    ax.yaxis.label.set_color("white")
    ax.xaxis.label.set_color("white")
    ax.title.set_color("white")

# ── Plot 1: Latency percentiles grouped bar ───────────────────────────────
ax1 = axes[0]
x = np.arange(len(percentiles))
width = 0.35

for i, (label, data) in enumerate(results.items()):
    vals = [data[p] for p in percentiles]
    bars = ax1.bar(x + i * width - width / 2, vals, width,
                   label=label.replace("\n", " "),
                   color=data["color"], alpha=0.9, zorder=3)
    for bar, v in zip(bars, vals):
        ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 8,
                 f"{v}", ha="center", va="bottom", fontsize=7.5,
                 color="white", fontweight="bold")

ax1.set_xticks(x)
ax1.set_xticklabels(labels, color="white")
ax1.set_ylabel("Latency (ms)", color="white")
ax1.set_title("Checkout Latency Percentiles", color="white", fontsize=13, pad=12)
ax1.set_ylim(0, 1200)
ax1.grid(axis="y", color="#2A2D3A", linewidth=0.8, zorder=0)
ax1.legend(facecolor="#1A1D27", edgecolor="#2A2D3A", labelcolor="white", fontsize=9)

# ── Plot 2: Throughput & success rate ────────────────────────────────────
ax2 = axes[1]

run_labels = ["100 users", "200 users"]
throughputs = [68.9, 133.1]
success_rates = [100 - 0.0, 100 - 0.01]  # 1 timeout in 7870 = 0.01%
colors = ["#4C9BE8", "#E85C4C"]

bars = ax2.bar(run_labels, throughputs, color=colors, alpha=0.9, width=0.45, zorder=3)
for bar, tp, sr in zip(bars, throughputs, success_rates):
    ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.5,
             f"{tp} req/s", ha="center", va="bottom",
             color="white", fontsize=11, fontweight="bold")
    ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() / 2,
             f"{sr:.2f}%\nsuccess", ha="center", va="center",
             color="white", fontsize=10, fontweight="bold")

ax2.set_ylabel("Requests / second", color="white")
ax2.set_title("Throughput & Real-error Rate\n(\"already paid\" = correct behavior, not an error)",
              color="white", fontsize=11, pad=12)
ax2.set_ylim(0, 170)
ax2.grid(axis="y", color="#2A2D3A", linewidth=0.8, zorder=0)
ax2.tick_params(colors="white")

# ── Annotation box ───────────────────────────────────────────────────────
note = (
    "Stack: 2 order + 2 stock + 2 payment instances\n"
    "3 Redis masters · 3 replicas · 3 Sentinels · Nginx gateway\n"
    "Protocol: adaptive 2PC / Saga  |  Lua atomic ops  |  WAL crash recovery"
)
fig.text(0.5, 0.01, note, ha="center", fontsize=8.5,
         color="#9BA3C0", style="italic")

plt.suptitle("Distributed Checkout System — Stress Test Results",
             color="white", fontsize=15, fontweight="bold", y=1.01)
plt.tight_layout()

out = "docs/stress_test_results.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"Saved: {out}")
plt.show()
