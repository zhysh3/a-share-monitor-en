#!/usr/bin/env python3
"""update_arisk_data.py — generated daily after market close: /home/zhuya/Desktop/arisk_data.json

Called by cron at 16:10 on trading days. Prefers MX API (social financing stock YoY), falls back to AKShare M2 YoY.
Every section has its own try/except; if one fails, the old JSON field is reused and the run continues.
"""
import json, os, sys, time, traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

OUT_PATH = os.environ.get('ARISK_OUT') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'arisk_data.json')
MX_KEY = os.environ.get('MX_APIKEY', '')

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Old JSON fallback ─────────────────────────────────────────────
def load_prev():
    try:
        with open(OUT_PATH) as f: return json.load(f)
    except Exception: return {}
PREV = load_prev()

def fallback(key, default=None):
    v = PREV.get(key)
    if v is None: return default
    log(f"  ↩ {key} reusing old value")
    return v

# ── 1. Social financing stock YoY ──────────────────────────────────────────
# Primary source: PBoC website “Aggregate Financing Stock” table; its“growth (%)”column is the social financing stock YoY.
#       Official PBoC figure, published around the 15th for the prior month; timelier than the MOFCOM mirror (AKShare shrzgm).
# Fallback:① MOFCOM mirror cumulative flow (old method, ~1pp high and lagging)② M2 YoY (IC≈0, placeholder).
TSF_BASELINE_201412 = 1228600   # 122.86 trillion = 1,228,600 100M yuan (PBoC Jan 2015 monetary policy report, old-method base)

PBOC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120 Safari/537.36")
PBOC_HOST = "https://www.pbc.gov.cn"

def _pboc_decode(b):
    for enc in ("utf-8", "gbk", "gb18030"):
        try: return b.decode(enc)
        except Exception: continue
    return b.decode("utf-8", "ignore")

def _pboc_cells(row_html):
    import re
    cs = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)
    return [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").replace("\xa0", " ").strip()
            for c in cs]

def pboc_year_tsf(year):
    """Fetch a year’s PBoC “Aggregate Financing Stock” table, returning [{'m':'YY-MM','g':YoY,'s':'pboc'}], published months only."""
    import re, requests
    h = {"User-Agent": PBOC_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    idx_url = f"{PBOC_HOST}/diaochatongjisi/116219/116319/{year}ntjsj/shrzgm/index.html"
    idx = _pboc_decode(requests.get(idx_url, headers=h, timeout=20).content)
    pos = idx.find("社会融资规模存量统计表")          # first attachDir htm after the label is the table
    if pos < 0:
        raise RuntimeError("annual index has no “Aggregate Financing Stock” table")
    m = re.search(r"/diaochatongjisi/attachDir/\d{4}/\d{2}/\d+\.htm", idx[pos:pos + 600])
    if not m:
        raise RuntimeError("stock table htm link not found")
    tbl = _pboc_decode(requests.get(PBOC_HOST + m.group(0), headers=h, timeout=20).content)
    months = re.findall(r"(20\d\d)\.(\d{1,2})", tbl)          # header months, in order
    total = None                                              # total row: first cell contains“aggregate financing stock”+“AFRE”
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S | re.I):
        c = _pboc_cells(row)
        if c and c[0].startswith("社会融资规模存量") and "AFRE" in c[0]:
            total = c; break
    if not total:
        raise RuntimeError("aggregate financing stock total row not found")
    pairs = list(zip(total[1::2], total[2::2]))               # (stock, growth) ×12
    out = []
    for (yy, mm), (_stock, yoy) in zip(months, pairs):
        try: g = float(yoy)
        except Exception: continue
        if 0 < g < 30:
            out.append({"m": f"{yy[2:]}-{int(mm):02d}", "g": round(g, 2), "s": "pboc"})
    return out

def _merge_pboc(fresh):
    """Merge newly fetched PBoC months with history already on the PBoC basis (s=pboc), dedupe by month, sort, keep the last 12.
    Old cumulative-method months are deliberately excluded to avoid a fake first-derivative jump at the seam."""
    cached = {e['m']: e for e in (PREV.get('m2_monthly') or [])
              if isinstance(e, dict) and e.get('s') == 'pboc'}
    for e in fresh:
        cached[e['m']] = e
    return sorted(cached.values(), key=lambda e: e['m'])[-12:]

def fetch_credit_yoy():
    # ── Primary: official PBoC social financing stock YoY (most authoritative and timely)──
    try:
        from datetime import date
        yr = date.today().year
        rows = pboc_year_tsf(yr)
        # Always try the prior year too so the chart isn’t left with only this year’s bars (2025 history is needed)
        try: rows = pboc_year_tsf(yr - 1) + rows
        except Exception as e: log(f"  · PBoC prior-year table unavailable: {e}")
        rows = _merge_pboc(rows)
        if len(rows) >= 3:
            log(f"  ✓ Social financing stock YoY[PBoC basis] ({len(rows)} months) latest {rows[-1]}  · PBoC direct")
            return rows
        log(f"  ✗ PBoC Social financing YoY: not enough points: {len(rows)} mo")
    except Exception as e:
        log(f"  ✗ PBoC Social financing YoY failed: {e}")
        traceback.print_exc()
    # ── Fallback 1: MOFCOM mirror cumulative flow (old method, ~1pp high and lagging)──
    try:
        import akshare as ak
        df = ak.macro_china_shrzgm()
        df = df.copy()
        df['月份'] = df['月份'].astype(str)
        df = df[df['月份'].str.match(r'^\d{6}$')].sort_values('月份').reset_index(drop=True)
        # cumulative stock = base + cumsum of flows
        df['增量'] = df['社会融资规模增量'].astype(float)
        df['存量'] = TSF_BASELINE_201412 + df['增量'].cumsum()
        # 12 -month YoY
        df['stock_lag12'] = df['存量'].shift(12)
        df['yoy'] = (df['存量'] / df['stock_lag12'] - 1) * 100
        df = df.dropna(subset=['yoy'])
        out = []
        for _, r in df.tail(14).iterrows():
            ym = r['月份']
            out.append({"m": f"{ym[2:4]}-{ym[4:6]}", "g": round(float(r['yoy']), 2)})
        if len(out) >= 6:
            log(f"  ⚠ Social financing stock YoY[fallback · MOFCOM cumulative] ({len(out)} months) latest {out[-1]}  · ~1pp high and lagging")
            return out[-12:]
        log(f"  ✗ Social financing stock YoY[fallback · cumulative] insufficient data: {len(out)} mo")
    except Exception as e:
        log(f"  ✗ Social financing stock YoY[fallback · cumulative] failed: {e}")
        traceback.print_exc()
    # Fallback: M2 YoY (IC≈0, placeholder only)
    try:
        import akshare as ak
        df = ak.macro_china_money_supply()
        date_col = next(c for c in df.columns if '月' in c or 'date' in c.lower())
        val_col = next(c for c in df.columns if 'M2' in c and '同比' in c)
        df = df.sort_values(date_col)
        out = []
        for _, row in df.tail(14).iterrows():
            raw = str(row[date_col]).strip()
            digits = ''.join(c for c in raw if c.isdigit())
            if len(digits) < 6: continue
            yy, mm = digits[2:4], digits[4:6]
            try: g = round(float(str(row[val_col]).replace('%','')), 2)
            except Exception: continue
            if 0 < g < 30: out.append({"m": f"{yy}-{mm}", "g": g})
        if len(out) >= 6:
            log(f"  ⚠ M2 YoY fallback (signal IC≈0) latest {out[-1]}")
            return out[-12:]
    except Exception as e:
        log(f"  ✗ M2 also failed: {e}")
    return fallback('m2_monthly')

# ── 2. 10Y treasury ──────────────────────────────────────────
def fetch_bond10y():
    try:
        import akshare as ak
        df = ak.bond_zh_us_rate()
        col = next(c for c in df.columns if '中国' in c and '10年' in c and '差' not in c)
        df = df[['日期', col]].dropna().sort_values('日期').tail(30)
        hist = [{"d": f"{d.month}/{d.day}", "v": round(float(v), 4)}
                for d, v in zip(__import__('pandas').to_datetime(df['日期']), df[col])]
        latest = round(float(df[col].iloc[-1]), 2)
        log(f"  ✓ bond10y latest={latest}% ({len(hist)} d)")
        return {"latest": latest, "hist": hist}
    except Exception as e:
        log(f"  ✗ bond10y failed: {e}")
        return fallback('bond10y')

# ── 3. CSI 300 PE ─────────────────────────────────────────
def fetch_pe_300():
    try:
        import akshare as ak
        df = ak.stock_index_pe_lg(symbol='沪深300')
        pe = round(float(df.iloc[-1]['滚动市盈率']), 2)
        log(f"  ✓ pe_300 = {pe}")
        return pe
    except Exception as e:
        log(f"  ✗ pe_300 failed: {e}")
        return fallback('pe_300')

# ── 4. HS300 HV30 ──────────────────────────────────────────
def fetch_hv30():
    try:
        import akshare as ak, math
        # Sina source is stable; Eastmoney endpoint sometimes RemoteDisconnected
        df = ak.stock_zh_index_daily(symbol="sh000300")
        df = df.sort_values('date').reset_index(drop=True)
        closes = df['close'].astype(float).tolist()
        dates = df['date'].astype(str).tolist()
        # 30 -day annualized HV
        hv_series = []
        for i in range(30, len(closes)):
            window = closes[i-30:i+1]
            log_rets = [math.log(window[j]/window[j-1]) for j in range(1, len(window))]
            mean = sum(log_rets)/len(log_rets)
            var = sum((r-mean)**2 for r in log_rets)/len(log_rets)
            hv_series.append(round(math.sqrt(var)*math.sqrt(252)*100, 1))
        hist_dates = dates[30:]
        # keep only the last 30 days for hist
        out_hist = [{"d": f"{int(d[5:7])}/{int(d[8:10])}", "v": v}
                    for d, v in zip(hist_dates[-30:], hv_series[-30:])]
        latest = hv_series[-1]
        # 5 -year percentile (HV distribution over the last ~1250 trading days)
        recent5y = hv_series[-1250:] if len(hv_series) >= 1250 else hv_series
        below = sum(1 for v in recent5y if v <= latest)
        pct = round(below / len(recent5y) * 100)
        log(f"  ✓ hv30 latest={latest}% pct={pct}% (based on {len(recent5y)} trading days)")
        return {"latest": latest, "pct": pct, "hist": out_hist}
    except Exception as e:
        log(f"  ✗ hv30 failed: {e}")
        traceback.print_exc()
        return fallback('hv30')

# ── 5. margin daily + monthly ─────────────────────────────────
def fetch_margin():
    try:
        import akshare as ak
        from datetime import timedelta
        today = datetime.now()
        start = (today - timedelta(days=400)).strftime('%Y%m%d')
        end = today.strftime('%Y%m%d')
        sse = ak.stock_margin_sse(start_date=start, end_date=end)
        sse_col = next((c for c in sse.columns if '余额' in c and '融资融券' in c), None) or '融资融券余额'
        date_col = next(c for c in sse.columns if '日期' in c)
        sse = sse[[date_col, sse_col]].rename(columns={date_col:'date', sse_col:'sse'})
        sse['date'] = sse['date'].astype(str).str[:8]
        try:
            szse = ak.stock_margin_szse(date=end)
            log(f"  · SZSE single-day query row count: {len(szse)}")
        except Exception:
            szse = None
        # Use SSE as primary, Shenzhen total ≈ ×1.85 (empirical ratio; avoids flaky SZSE multi-day endpoint)
        sse_all = sse.sort_values('date').reset_index(drop=True)
        sse_all['sse'] = sse_all['sse'].astype(float)
        sse_all['total'] = sse_all['sse'] * 1.85
        # daily last 30 days
        sse_daily = sse_all.tail(30)
        daily = [{"d": f"{int(d[4:6])}/{int(d[6:8])}", "v": int(round(float(v)/1e8))}
                 for d, v in zip(sse_daily['date'].tolist(), sse_daily['total'].tolist())]
        # monthly group all data by YYYY-MM and take each month’s last day
        # Key: drop the"current incomplete month"so a mid-month value isn’t treated as"month-end", which would distort the Z-score
        sse_all['ym'] = sse_all['date'].str[:6]
        current_ym = today.strftime('%Y%m')
        last_by_month = sse_all.groupby('ym').last().reset_index()
        # keep only ym < current month"completed"month
        completed = last_by_month[last_by_month['ym'] < current_ym]
        monthly = [{"m": f"{r['ym'][2:4]}-{r['ym'][4:6]}", "v": int(round(r['total']/1e8))}
                   for _, r in completed.iterrows()]
        monthly = monthly[-12:]
        # record the"month-to-date"value to current_month (not used in Z, but shown on the dashboard)
        cur_row = last_by_month[last_by_month['ym'] == current_ym]
        current_month = None
        if not cur_row.empty:
            r = cur_row.iloc[0]
            current_month = {"m": f"{r['ym'][2:4]}-{r['ym'][4:6]}",
                             "v": int(round(r['total']/1e8)),
                             "partial": True,
                             "as_of": daily[-1]['d'] if daily else None}
        log(f"  ✓ margin daily={len(daily)} monthly={len(monthly)}(completed) "
            f"{'+current incomplete month ' + current_month['m'] if current_month else ''}"
            f"latest balance {daily[-1]['v']} 100M yuan")
        out = {"daily": daily[-30:], "monthly": monthly[-12:]}
        if current_month: out["current_month"] = current_month
        return out
    except Exception as e:
        log(f"  ✗ margin failed: {e}")
        return fallback('margin')

# ── 6. 7-day turnover ────────────────────────────────────────
def fetch_vol_7d():
    """Sina source stock_zh_index_daily + Empirical conversion factor (same as proxy.py, avoids Eastmoney TLS limits)"""
    try:
        import akshare as ak
        AMT_FACTOR = {"sh000001": 19.1, "sz399001": 20.8}
        def _close_vol(sym):
            df = ak.stock_zh_index_daily(symbol=sym).sort_values('date').tail(7).reset_index(drop=True)
            df['date'] = df['date'].astype(str)
            return df
        sh = _close_vol("sh000001")
        sz = _close_vol("sz399001")
        n = min(len(sh), len(sz))
        out = []
        for i in range(n):
            d = sh.iloc[i]['date']
            # estimated turnover (100M yuan) = close × volume(shares) × factor / 1e8
            amt_sh = float(sh.iloc[i]['close']) * float(sh.iloc[i]['volume']) * AMT_FACTOR['sh000001'] / 1e8
            amt_sz = float(sz.iloc[i]['close']) * float(sz.iloc[i]['volume']) * AMT_FACTOR['sz399001'] / 1e8
            # that estimate runs high; measured: amt ≈ volume(shares) × avgPrice ≈ volume × close / 100
            # actual: combined SH+SZ daily turnover≈ 1-2 trillion, volume sh000001 is 6-8 billion shares, close ~4000 → close*vol=2.5e14
            # simplified: measured amount/volume ratio is roughly 19-21 (avg yuan/share)
            # use empirical factor: amt = volume × factor (yuan), factor from above AMT_FACTOR
            amt_sh = float(sh.iloc[i]['volume']) * AMT_FACTOR['sh000001'] / 1e8
            amt_sz = float(sz.iloc[i]['volume']) * AMT_FACTOR['sz399001'] / 1e8
            total = amt_sh + amt_sz
            out.append({"d": f"{int(d[5:7])}/{int(d[8:10])}", "v": int(round(total))})
        log(f"  ✓ vol_7d {len(out)} days (Sina estimate), latest {out[-1]['v']} 100M yuan")
        return out
    except Exception as e:
        log(f"  ✗ vol_7d failed: {e}")
        traceback.print_exc()
        return fallback('vol_7d')

# ── 7. 5-day limit-up/limit-down counts ─────────────────────────────────────
def fetch_limit_7d():
    try:
        import akshare as ak
        from datetime import timedelta
        # scan the last 10 calendar days to find 5 trading days
        out, dt = [], datetime.now()
        for _ in range(15):
            ds = dt.strftime('%Y%m%d')
            try:
                up = len(ak.stock_zt_pool_em(date=ds))
                dn = len(ak.stock_zt_pool_dtgc_em(date=ds))
                if up > 0 or dn > 0:
                    out.append({"date": dt.strftime('%Y-%m-%d'), "up": up, "down": dn})
                    if len(out) >= 5: break
            except Exception: pass
            dt -= timedelta(days=1)
        out.reverse()
        log(f"  ✓ limit_7d {len(out)} days, today up/dn={out[-1]['up']}/{out[-1]['down']}")
        return out
    except Exception as e:
        log(f"  ✗ limit_7d failed: {e}")
        return fallback('limit_7d')

# ── 8. Shenwan L1 60-day change ────────────────────────────────────
def fetch_sector_live():
    try:
        import akshare as ak
        info = ak.sw_index_first_info()
        items = [(row['行业代码'].split('.')[0], row['行业名称']) for _, row in info.iterrows()]

        def _one(code, name):
            try:
                df = ak.index_hist_sw(symbol=code, period='day')
                df = df.sort_values('日期').reset_index(drop=True)
                closes = df['收盘'].astype(float).tolist()
                if len(closes) < 61: return None
                ret60 = (closes[-1]/closes[-61]-1)*100
                today = (closes[-1]/closes[-2]-1)*100
                return {"n": name, "code": code,
                        "excess": round(today, 2), "ret60": round(ret60, 2),
                        "date": str(df['日期'].iloc[-1])[:10]}
            except Exception: return None

        out = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_one, c, n): (c, n) for c, n in items}
            for f in as_completed(futs):
                r = f.result()
                if r: out.append(r)
        out.sort(key=lambda x: x['ret60'], reverse=True)
        log(f"  ✓ sector_live {len(out)}/{len(items)} sectors, strongest {out[0]['n']} {out[0]['ret60']}%")
        return out
    except Exception as e:
        log(f"  ✗ sector_live failed: {e}")
        return fallback('sector_live')

# ── 9. All-A turnover rate (SH + SZ combined)─────────────────────────────
# Used by dashboard L2 pcaCrowding; previously the dashboard could only estimate via estimateTurn(volYuan)
def _turnover_one_day(d):
    """single-day All-A turnover rate. d = 'YYYYMMDD'. Raises on failure."""
    import akshare as ak
    sh = ak.stock_sse_deal_daily(date=d)
    sz = ak.stock_szse_summary(date=d)
    if sh.empty or sz.empty:
        raise ValueError("empty frame")
    # SH: Main Board A + STAR Market (already in 100M yuan)
    sh_amt_row = sh[sh['单日情况'] == '成交金额']
    sh_cap_row = sh[sh['单日情况'] == '流通市值']
    sh_amt = float(sh_amt_row['主板A'].iloc[0]) + float(sh_amt_row['科创板'].iloc[0])
    sh_cap = float(sh_cap_row['主板A'].iloc[0]) + float(sh_cap_row['科创板'].iloc[0])
    # SZ: Main Board A + ChiNext A (in yuan, divide by 1e8)
    sz_a = sz[sz['证券类别'].isin(['主板A股', '创业板A股'])]
    sz_amt = float(sz_a['成交金额'].sum()) / 1e8
    sz_cap = float(sz_a['流通市值'].sum()) / 1e8
    total_amt, total_cap = sh_amt + sz_amt, sh_cap + sz_cap
    return {
        "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
        "label": f"{int(d[4:6])}/{int(d[6:8])}",
        "sh_amount_yi": round(sh_amt),
        "sz_amount_yi": round(sz_amt),
        "sh_mktcap_yi": round(sh_cap),
        "sz_mktcap_yi": round(sz_cap),
        "amount_yi": round(total_amt),
        "mktcap_yi": round(total_cap),
        "pct": round(total_amt / total_cap * 100, 3),
    }

def fetch_turnover():
    """All-A turnover series for the last 7 trading days = SH+SZ turnover / SH+SZ A-share free-float cap × 100.

    Returns {..latest-day fields.., 'avg_pct': latest value, 'series': [{label,date,pct,amount_yi}, ...×7]}
    series feeds the dashboard’s main panel"Turnover × Turnover Rate"chart (previously the chart relied on a
    hardcoded 9e13 free-float estimate, about 8% off the real figure).
    """
    try:
        from datetime import timedelta
        days, cursor = [], datetime.now()
        # scan back up to 20 calendar days to collect 7 trading days
        for _ in range(20):
            d = cursor.strftime('%Y%m%d')
            try:
                days.append(_turnover_one_day(d))
                if len(days) >= 7: break
            except Exception:
                pass
            cursor -= timedelta(days=1)
        if not days:
            log("  ✗ turnover 20 days: no trading-day data available")
            return fallback('turnover')
        days.reverse()                       # oldest to newest
        latest = days[-1]
        out = dict(latest)
        out['avg_pct'] = latest['pct']       # backward-compatible old field names
        out['series'] = [{"label": x['label'], "date": x['date'],
                          "pct": x['pct'], "amount_yi": x['amount_yi']} for x in days]
        log(f"  ✓ turnover {latest['date']} = {latest['pct']}% "
            f" ({latest['amount_yi']}100M yuan/{latest['mktcap_yi']}100M yuan), series {len(days)} days")
        return out
    except Exception as e:
        log(f"  ✗ turnover failed: {e}")
        traceback.print_exc()
    return fallback('turnover')

# ── 10. New equity fund issuance ───────────────────────────────────────
def fetch_fund_issuance():
    try:
        import akshare as ak
        df = ak.fund_new_found_em()
        col_date = next(c for c in df.columns if '成立' in c or '日期' in c)
        col_share = next(c for c in df.columns if '份额' in c)
        col_type = next((c for c in df.columns if '类型' in c), None)
        if col_type:
            df = df[df[col_type].astype(str).str.contains('股票|混合', na=False)]
        df = df.dropna(subset=[col_date, col_share]).copy()
        import pandas as pd
        df[col_date] = pd.to_datetime(df[col_date], errors='coerce')
        df = df.dropna(subset=[col_date])
        df['ym'] = df[col_date].dt.strftime('%y-%m')
        df[col_share] = pd.to_numeric(df[col_share], errors='coerce')
        agg = df.groupby('ym')[col_share].sum().sort_index().tail(12)
        out = [{"m": ym, "v": round(float(v), 1)} for ym, v in agg.items()]
        log(f"  ✓ fund_issuance {len(out)} months, latest {out[-1] if out else 'empty'}")
        return out
    except Exception as e:
        log(f"  ✗ fund_issuance failed: {e}")
        return fallback('fund_issuance')

# ── 11. ETF flows by category (SSE, 60-trading-day net inflow)───────────────
# Source: SSE ETF shares (ak.fund_etf_scale_sse, snapshot by STAT_DATE ),
#   price from ak.fund_etf_spot_em. Net inflow ≈ (shares_now − shares_60d ago) × price.
#   old shares valued at today’s price to strip out price effects, change_pct = net inflow / old value.
#   Category is assigned by keywords in the fund’s short name; sector themes take priority over broad-based; “Other” is about 0-2%.
ETF_RULES = [
    ("增强指数", ["增强"]),
    ("跨境",     ["恒生","恒指","恒","中概","港股","H股","HK","HKC","纳指","纳斯达克","标普","日经",
                  "德国","DAX","道琼斯","海外","美股","东南亚","亚太","越南","印度","法国","沙特",
                  "新兴市场","全球","中韩","日本","欧洲","香港","东证","NA股","沪港深","港科","巴西",
                  "新兴亚洲","亚洲","中金优","MSCI中国","富时中国"]),
    ("商品",     ["黄金","白银","原油","豆粕","能源化工","农产品","大宗","饲料","生猪期","有色金属期",
                  "上海金","金ETF","商品"]),
    ("债券货币", ["可转债","转债","国债","政金债","信用债","城投债","货币","债ETF","短融","国开","地方债",
                  "现金基金","现金指数","现金ETF","中银现金","活期","添益","现金添","债"]),
    ("半导体芯片",["芯片","半导","存储","封测","集成电路","科创芯","芯","科创材料","科创新材","电子"]),
    ("AI算力",   ["人工智能","算力","云计算","大数据","数字经济","机器人","数据中心","AI","数据","数字"]),
    ("软件通信", ["软件","计算机","信创","网络安全","通信","5G","物联网","游戏","传媒","互联网","网络",
                  "云","信息","TMT","科技","文娱","影视","电信"]),
    ("医药生物", ["医药","医疗","创新药","疫苗","中药","基因","生物","疫","CXO","器械","医",
                  "新药","生科","保健","养老","药"]),
    ("新能源电力",["新能源","电池","锂电","光伏","储能","风电","电网","电力","绿电","核电","氢能",
                  "新能车","碳中和","新能","双碳","公用","能源","低碳","绿色"]),
    ("汽车交运", ["汽车","整车","零部件","物流","运输","港口","航运","铁路","公路","交通","交运","车",
                  "智能驾驶","驾驶"]),
    ("消费",     ["白酒","食品","饮料","家电","家居","旅游","免税","零售","纺织","服装","农业","养殖",
                  "消费","畜牧","乳","酒","农牧","宠物","美容","农林牧渔","教育","消服","消电","国货"]),
    ("金融地产", ["银行","证券","券商","保险","地产","REIT","金融","房","不动产"]),
    ("军工制造", ["军工","国防","航空","航天","卫星","船舶","机械","装备","专精特新","工业母机",
                  "高端制造","兵","导弹","国防军工","智能制造","智造","通航"]),
    ("周期资源", ["有色","煤炭","钢铁","化工","矿","稀土","稀有","石油","石化","建材","水泥","资源",
                  "材料","钢","煤","油气","电解铝","锂","环保","金属","新材","基建"]),
    ("风格因子", ["红利","低波","价值","成长","质量","动量","ESG","自由现金流","现金流","现金自由",
                  "自由现金","基本面","央企","国企","国资","央创","龙头","蓝筹","分红","股息","价值回报",
                  "可持续","央调","央视"]),
    ("宽基",     ["沪深300","中证500","上证50","中证1000","中证2000","A500","中证A50","科创50",
                  "科创100","科创综","创业板","上证综指","上证指数","深证","中证100","中证800","双创",
                  "国证","巨潮","中证全指","上证180","上证380","MSCI","富时","综指","规模",
                  "治理","超大","中盘","大盘","小盘","上证","中证","沪深","全指","战略新兴","产业升级",
                  "长三角","湾区","G60","之江","综合","龙头股","央视50","长江","张江","科创","科综","科200","A股",
                  "300","500","1000","2000","50","800","180","100","380","225","580"]),
]

def _etf_classify(name):
    for cat, kws in ETF_RULES:
        for kw in kws:
            if kw in name:
                return cat
    return "其他"

def _sse_scale_on(date_str):
    """SSE ETF shares DataFrame for a trading day; None if empty or failed. date_str='YYYYMMDD'"""
    try:
        import akshare as ak
        df = ak.fund_etf_scale_sse(date=date_str)
        return df if (df is not None and len(df) > 100) else None
    except Exception:
        return None

def _sse_find_valid(anchor, back_days):
    """From anchor, look back up to back_days days for a day with SSE data; returns (df, 'YYYY-MM-DD')"""
    from datetime import timedelta
    cur = anchor
    for _ in range(back_days):
        df = _sse_scale_on(cur.strftime('%Y%m%d'))
        if df is not None:
            return df, cur.strftime('%Y-%m-%d')
        cur -= timedelta(days=1)
    return None, None

def fetch_etf_categories():
    try:
        import akshare as ak
        from datetime import timedelta
        from collections import defaultdict
        # price
        spot = ak.fund_etf_spot_em()
        price = {str(r['代码']): float(r['最新价']) for _, r in spot.iterrows()
                 if r['最新价'] and float(r['最新价']) > 0}
        # two share snapshots: latest + ~60 trading days ago
        df_now, date_now = _sse_find_valid(datetime.now(), 8)
        if df_now is None:
            log("  ✗ etf_categories: SSE 最新份额不可得")
            return fallback('etf_categories')
        anchor_old = datetime.strptime(date_now, '%Y-%m-%d') - timedelta(days=88)
        df_old, date_old = _sse_find_valid(anchor_old, 12)
        old_share = ({str(r['基金代码']): float(r['基金份额']) for _, r in df_old.iterrows()}
                     if df_old is not None else {})

        agg = defaultdict(lambda: {"count": 0, "scale_now": 0.0, "scale_old": 0.0, "flow": 0.0})
        for _, r in df_now.iterrows():
            code = str(r['基金代码']); name = str(r['基金简称'])
            p = price.get(code)
            if not p:
                continue
            sh_now = float(r['基金份额'])
            sh_old = old_share.get(code, sh_now)      # newly listed, no old value → counts as 0 inflow
            scale_now = sh_now * p / 1e8              # 100M yuan
            scale_old = sh_old * p / 1e8              # old shares at current price
            a = agg[_etf_classify(name)]
            a["count"] += 1; a["scale_now"] += scale_now
            a["scale_old"] += scale_old; a["flow"] += (scale_now - scale_old)

        cats = []
        for cat, a in agg.items():
            pct = (a["flow"] / a["scale_old"] * 100) if a["scale_old"] > 0 else 0.0
            cats.append({"category": cat, "count_now": a["count"],
                         "scale_yi": round(a["scale_now"], 1),
                         "change_yi": round(a["flow"], 1),
                         "change_pct": round(pct, 2)})
        cats.sort(key=lambda x: x["change_yi"], reverse=True)
        total = sum(c["scale_yi"] for c in cats) or 1
        other = next((c["scale_yi"] for c in cats if c["category"] == "其他"), 0)
        log(f"  ✓ etf_categories now={date_now} vs {date_old} "
            f"{len(cats)}categories/{sum(c['count_now'] for c in cats)}funds, Other share {other/total*100:.1f}%")
        return {"latest_date": date_now, "prev_date": date_old, "categories": cats}
    except Exception as e:
        log(f"  ✗ etf_categories failed: {e}")
        traceback.print_exc()
        return fallback('etf_categories')

# ── Main flow ────────────────────────────────────────────────
def main():
    t0 = time.time()
    log("=== update_arisk_data.py Start ===")
    log(f"  MX_APIKEY: {'configured' if MX_KEY else 'not configured (AKShare fallback only)'}")

    out = {
        "generated_at": datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        "generated_date": datetime.now().strftime('%Y-%m-%d'),
    }

    log("[1/11] Fetch social financing stock YoY / M2 ...")
    out['m2_monthly'] = fetch_credit_yoy()
    log("[2/11] Fetch 10Y treasury ...")
    out['bond10y'] = fetch_bond10y()
    log("[3/11] Fetch CSI 300 PE ...")
    out['pe_300'] = fetch_pe_300()
    log("[4/11] Compute HV30 ...")
    out['hv30'] = fetch_hv30()
    log("[5/11] Fetch margin ...")
    out['margin'] = fetch_margin()
    log("[6/11] Fetch 7-day turnover ...")
    out['vol_7d'] = fetch_vol_7d()
    log("[7/11] Fetch 5-day limit-up/down ...")
    out['limit_7d'] = fetch_limit_7d()
    log("[8/11] Fetch Shenwan 31-sector 60-day ...")
    out['sector_live'] = fetch_sector_live()
    log("[9/11] Fetch All-A turnover rate ...")
    out['turnover'] = fetch_turnover()
    log("[10/11] Fetch new equity fund issuance ...")
    out['fund_issuance'] = fetch_fund_issuance()
    log("[11/11] Fetch ETF flows by category (SSE 60-day)...")
    out['etf_categories'] = fetch_etf_categories()

    # keep None fields (when fallback fails) but log them
    missing = [k for k, v in out.items() if v is None]
    if missing: log(f"⚠ missing fields: {missing}")

    # Wrote
    tmp = OUT_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_PATH)
    log(f"=== done ({time.time()-t0:.1f}s), output {OUT_PATH} ===")

if __name__ == '__main__':
    sys.exit(main())
