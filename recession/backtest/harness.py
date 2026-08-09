"""S3 backtest → backtest_report.html (spec §3.4, §12). Rendering only; all the
metrics/data-loading live in metrics.py (matplotlib-free) so the API can reuse them.

    python -m recession.backtest.harness
"""
from __future__ import annotations

import base64
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from recession import config
from recession.backtest import metrics as M
from recession.backtest.metrics import (DD_RISK_BAR, DD_WINDOW, FP_HORIZON, WARN)


def _b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _shade(ax, usrec):
    v = usrec.astype(bool).to_numpy(); idx = usrec.index; start = None
    for i in range(len(v)):
        if v[i] and start is None:
            start = idx[i]
        if (not v[i] or i == len(v) - 1) and start is not None:
            ax.axvspan(start, idx[i], color="#888", alpha=0.18, lw=0); start = None


def _composite_fig(modes, usrec):
    fig, ax = plt.subplots(figsize=(12, 4.2))
    _shade(ax, usrec)
    modes["revised"]["composite"].plot(ax=ax, color="#f2695f", lw=1.2, label="Revised")
    modes["pit"]["composite"].plot(ax=ax, color="#5b97f5", lw=1.6, label="PIT (release-lag)")
    ax.axhline(WARN, color="#e3ab4f", ls="--", lw=1.0, label=f"trigger {WARN:.0f}")
    ax.set_ylim(0, 100); ax.set_ylabel("composite 0–100")
    ax.set_title("Market-risk / recession-pressure meter vs NBER recessions (grey)")
    ax.legend(loc="upper left", fontsize=8)
    return _b64(fig)


def _fan_fig(env, usrec):
    fig, ax = plt.subplots(figsize=(12, 3.2))
    _shade(ax, usrec)
    ax.fill_between(env.index, env["lo"], env["hi"], color="#5b97f5", alpha=0.22, label="±50% weight envelope")
    env["base"].plot(ax=ax, color="#5b97f5", lw=1.3, label="base weights")
    ax.axhline(WARN, color="#e3ab4f", ls="--", lw=0.8); ax.set_ylim(0, 100)
    ax.set_title("Weight sensitivity (±50% per section)"); ax.legend(loc="upper left", fontsize=8)
    return _b64(fig)


def render() -> None:
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    modes = M.load_modes(); frame = modes.pop("_frame")
    idx = modes["revised"].index
    usrec = M.usrec_monthly(idx); fdd = M.forward_drawdown(idx)
    comp_pit = modes["pit"]["composite"]

    leads_r, leads_p = M.lead_times(modes["revised"]["composite"]), M.lead_times(comp_pit)
    fp_rows = M.false_positives(comp_pit, usrec, fdd)
    dd_card = M.drawdown_scorecard(comp_pit, fdd)
    auc = M.signal_auc(frame, usrec)
    env = M.weight_envelope(frame)
    rec_v, rec_t = M.recession_verdict(leads_p, fp_rows)
    risk_v, risk_t = M.risk_verdict(dd_card, fp_rows)
    gap = (modes["revised"]["composite"] - comp_pit).abs().mean()

    lead_rows = "".join(
        f"<tr><td>{lr['name']}</td><td>{'✓' if lp['hit'] else '—'}</td>"
        f"<td>{lr['lead'] if lr['lead'] is not None else '—'}</td>"
        f"<td>{lp['lead'] if lp['lead'] is not None else '—'}</td></tr>"
        for lr, lp in zip(leads_r, leads_p))
    dd_rows = "".join(
        f"<tr><td>≥ {r['bar']}% drawdown</td><td>{r['base']:.0f}%</td>"
        f"<td><b>{r['prec']:.0f}%</b></td><td>{r['lift']}×</td><td>{r['auc']:.2f}</td></tr>"
        for r in dd_card)

    def _fp_row(f):
        dd = "" if f["dd"] is None else f"{f['dd']:.0f}%"
        rec = "yes" if f["recession"] else "—"
        verdict = "✓ risk-useful" if (f["recession"] or f["risk_useful"]) else "🔴 genuine false alarm"
        return f"<tr><td>{f['date']}</td><td>{rec}</td><td>{dd}</td><td>{verdict}</td></tr>"
    fp_annot = "".join(_fp_row(f) for f in fp_rows)
    auc_rows = "".join(
        f"<tr><td>{r.series}</td><td>{r.section}</td><td>{r.auc6:.2f}</td>"
        f"<td>{r.auc12:.2f}</td><td>{r.auc18:.2f}</td></tr>" for r in auc.itertuples())

    col = {"GO": "#40c46a", "TUNE": "#e3ab4f", "MARGINAL": "#e3ab4f", "NO-GO": "#f2695f"}
    html = f"""<!doctype html><meta charset=utf-8>
<title>Market-risk / recession-pressure gauge — Backtest</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:1080px;margin:24px auto;padding:0 18px;color:#e6e6e6;background:#0b0e14}}
 h1,h2{{font-weight:650}} h2{{margin-top:30px;border-bottom:1px solid #222;padding-bottom:6px}}
 .vwrap{{display:flex;gap:14px;flex-wrap:wrap;margin:12px 0}}
 .verdict{{flex:1;min-width:320px;border-radius:12px;padding:13px 16px;border:1px solid}}
 .verdict .lbl{{font-size:10px;letter-spacing:.6px;text-transform:uppercase;color:#8a94a6}}
 .verdict .v{{font-size:20px;font-weight:800}}
 table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}}
 th,td{{border-bottom:1px solid #222;padding:5px 9px;text-align:right}}
 th:first-child,td:first-child,td:nth-child(2){{text-align:left}}
 img{{width:100%;border:1px solid #222;border-radius:8px;margin:6px 0}} .muted{{color:#8a94a6}}
</style>
<h1>Market-risk / recession-pressure gauge <span class=muted>— backtest (25-series slice)</span></h1>
<div class=vwrap>
 <div class=verdict style="border-color:{col[risk_v]}"><div class=lbl>Market-risk lens (get-defensive)</div>
   <div class=v style="color:{col[risk_v]}">{risk_v}</div><div class=muted>{risk_t}</div></div>
 <div class=verdict style="border-color:{col[rec_v]}"><div class=lbl>Recession lens (NBER)</div>
   <div class=v style="color:{col[rec_v]}">{rec_v}</div><div class=muted>{rec_t}</div></div>
</div>
<p class=muted>Trigger level {WARN:.0f}. PIT = release-lag (mean |revised−PIT| {gap:.1f} pts).</p>
<h2>The meter — PIT vs revised, NBER recessions shaded</h2>
<img src="data:image/png;base64,{_composite_fig(modes, usrec)}">
<h2>Market-risk scorecard — meter ≥ {WARN:.0f} vs forward {DD_WINDOW}mo drawdown</h2>
<table><tr><th>Event</th><th>base rate</th><th>precision @{WARN:.0f}</th><th>lift</th><th>AUC</th></tr>{dd_rows}</table>
<h2>Recession lead time (months to NBER peak)</h2>
<table><tr><th>Recession</th><th>Led</th><th>Revised</th><th>PIT</th></tr>{lead_rows}</table>
<h2>Every {WARN:.0f}-crossing — recession OR drawdown that followed?</h2>
<table><tr><th>Crossing</th><th>Recession ≤{FP_HORIZON}mo</th><th>Fwd {DD_WINDOW}mo trough</th><th>Verdict</th></tr>{fp_annot}</table>
<p class=muted>"Genuine false alarm" = neither a recession nor a ≥{DD_RISK_BAR}% drawdown followed.</p>
<h2>Per-signal AUC (recession within horizon)</h2>
<table><tr><th>Series</th><th>Section</th><th>6mo</th><th>12mo</th><th>18mo</th></tr>{auc_rows}</table>
<h2>Weight sensitivity</h2>
<img src="data:image/png;base64,{_fan_fig(env, usrec)}">
<p class=muted>Weights are frozen spec priors — robustness check, not a tuning knob.</p>
"""
    out = config.REPORT_DIR / "backtest_report.html"
    out.write_text(html)
    genuine = [f["date"] for f in fp_rows if f["genuine"]]
    print(f"\n{'='*56}\nMARKET-RISK: {risk_v} — {risk_t}\nRECESSION:  {rec_v} — {rec_t}")
    print(f"Genuine false alarms: {genuine or 'none'}\nReport: {out}")


if __name__ == "__main__":
    render()
