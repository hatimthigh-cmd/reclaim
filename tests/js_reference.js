// Reference: the exact JavaScript engine used for the research backtest (sigY + simV),
// run under node on the same candles as the Python bot for a parity check.
const fs = require('fs');
const ema = (a, n) => { const m = 2 / (n + 1); let e = a[0]; return a.map(x => e = x * m + e * (1 - m)); };
const prepY = (b) => {
  const n = b.length, T = b.map(x => x[0]), O = b.map(x => x[1]), H = b.map(x => x[2]), L = b.map(x => x[3]), C = b.map(x => x[4]);
  const e200 = ema(C, 200);
  const atr = new Array(n); let a = 0;
  for (let i = 0; i < n; i++) { const tr = i ? Math.max(H[i] - L[i], Math.abs(H[i] - C[i - 1]), Math.abs(L[i] - C[i - 1])) : H[i] - L[i]; a = i < 14 ? (a * i + tr) / (i + 1) : (a * 13 + tr) / 14; atr[i] = a; }
  return { n, T, O, H, L, C, e200, atr };
};
const sigY = (P, win = 8) => {
  const { n, T, H, L, C, e200, atr } = P, out = [], dq = [];
  let curPh = -1, base = null, LL = Infinity, li = -1, rcDone = -1;
  for (let i = 1; i < n; i++) {
    const j = i - 1; while (dq.length && H[dq[dq.length - 1]] <= H[j]) dq.pop(); dq.push(j); while (dq[0] < i - 120) dq.shift();
    const ph = dq[0];
    if (ph !== curPh) { curPh = ph; base = null; LL = Infinity; li = -1; for (let k = ph + 1; k < i; k++) if (L[k] < LL) { LL = L[k]; li = k; } rcDone = -1; }
    if (L[i] < LL) { LL = L[i]; li = i; }
    if (i < 900 || i - ph < 2 || li < 0) continue;
    const hi = H[ph], dd = 1 - LL / hi; if (dd < 0.25) continue;
    if (base === null) { base = Infinity; for (let k = Math.max(0, ph - 168); k < ph; k++) base = Math.min(base, L[k]); }
    if (hi / base - 1 < 0.4) continue;
    if (li >= i - win && li < i && rcDone !== li && C[i] > e200[i] && C[i - 1] <= e200[i - 1]) { out.push({ i, li, flushX: (H[li] - L[li]) / (atr[li - 1] || atr[li]) }); rcDone = li; }
  }
  return out;
};
const simV = (P, sigs, o) => {
  const c = Object.assign({ atrM: 1.5, tp: 1.5, retest: 48, maxHold: 168, cost: 0.0010, flushXMin: 0, maxStop: 0.25 }, o);
  const trades = []; let busy = 0;
  for (const s of sigs) {
    if (P.T[s.i] < busy || s.flushX < c.flushXMin) continue;
    let f = -1, en;
    for (let k = s.i + 1; k < P.n && k <= s.i + c.retest; k++) { const px = P.e200[k - 1]; if (P.L[k] <= px) { f = k; en = Math.min(P.O[k], px); break; } }
    if (f < 0) continue;
    const sl = en - c.atrM * P.atr[f - 1]; if (!(sl < en)) continue; const risk = (en - sl) / en; if (risk > c.maxStop || risk < 0.004) continue;
    const R = en - sl, tp = en + c.tp * R, end = Math.min(P.n - 1, f + c.maxHold); let out = null, k;
    if (P.L[f] <= sl) { out = -1; k = f; } else for (k = f + 1; k <= end; k++) { if (P.L[k] <= sl) { out = (Math.min(P.O[k], sl) - en) / R; break; } if (P.H[k] >= tp) { out = c.tp; break; } }
    const open = out === null && f + c.maxHold > P.n - 1; if (out === null) { k = end; out = (P.C[end] - en) / R; }
    trades.push({ i: s.i, f, k, en, sl, R: out - c.cost / risk, open });
    busy = P.T[k] + 3600e3;
  }
  return trades;
};
const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const opts = JSON.parse(process.argv[3] || '{"flushXMin":2.0,"maxStop":0.12}');
const out = {};
for (const [name, bars] of Object.entries(data)) {
  const P = prepY(bars), sigs = sigY(P);
  out[name] = { signals: sigs.map(s => [s.i, s.li, s.flushX]), trades: simV(P, sigs, opts) };
}
process.stdout.write(JSON.stringify(out));
