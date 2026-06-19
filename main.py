# =====================================================================
# SECTION 1: প্রয়োজনীয় লাইব্রেরি ইম্পোর্ট ও গ্লোবাল সেটিংস
# =====================================================================
import ccxt
import pandas as pd
import ta
import time
import threading
import json
import os
from flask import Flask, render_template_string, jsonify
from datetime import datetime, timezone

# ট্রেডিং পেয়ার এবং ফাইলের সেটিংস
SYMBOL = "SOL/USDT"
STATE_FILE = "bot_state.json"
INITIAL_FUND = 100.0

# ট্রেন্ড/সুইং ট্রেডিং সেটিংস (বড় প্রফিট এবং স্ট্যান্ডার্ড স্টপ লস)
DEF_TP = 0.035  # ৩.৫% টেক প্রফিট (বড় প্রফিট বুকিং)
DEF_SL = 0.020  # ২.০% স্টপ লস (ট্রেডকে নড়াচড়ার পর্যাপ্ত সুযোগ দেওয়া)

# থ্রেড লক
STATE_LOCK = threading.Lock()

# এক্সচেঞ্জ কানেকশন
exchange = ccxt.bitget({'enableRateLimit': True})

# ডিফল্ট স্টেট (ট্রেন্ড ট্রেডিং মডেল অনুযায়ী সাজানো)
DEFAULT_STATE = {
    "price": 0.0,
    "balance": INITIAL_FUND,
    "total_pnl": 0.0,
    "last_update": "...",
    "trades": 0,
    "win_rate": 0,
    "best": 0.0,
    "worst": 0.0,
    "last_action": "---",
    "in_position": False,
    "live_pnl_pct": 0.0,
    "live_pnl_val": 0.0,
    "entry_price": 0.0,
    "sl_level": 0.0,
    "tp_level": 0.0,
    "analysis_15m": {"rsi": 0, "ema20": 0, "ema50": 0, "sig": "লোড হচ্ছে...", "pats": []},
    "analysis_1h": {"rsi": 0, "ema200": 0, "sig": "লোড হচ্ছে...", "pats": []},
    "wait_reason": "লোড হচ্ছে...",
    "log": [],
    "history": []
}

# ডিস্ক ল্যাগ এড়াতে মেমোরি ক্যাশ ভেরিয়েবল
LAST_LOADED_TIME = 0
CACHED_STATE = DEFAULT_STATE.copy()


# =====================================================================
# SECTION 2: অপ্টিমাইজড থ্রেড-সেফ ক্যাশ ফাইল ম্যানেজমেন্ট (No-Lag Disk Read)
# =====================================================================
def save_state(d):
    """নিরাপদভাবে ফাইল সেভ করে"""
    with STATE_LOCK:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(d, f)
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error saving state: {e}")


def load_state():
    """ফাইল মডিফিকেশন চেক করে ক্যাশ থেকে ইনস্ট্যান্ট লোড করে"""
    global LAST_LOADED_TIME, CACHED_STATE
    with STATE_LOCK:
        if not os.path.exists(STATE_FILE):
            return DEFAULT_STATE.copy()
        try:
            mtime = os.path.getmtime(STATE_FILE)
            if mtime > LAST_LOADED_TIME:
                with open(STATE_FILE, "r") as f:
                    CACHED_STATE = json.load(f)
                LAST_LOADED_TIME = mtime
            return CACHED_STATE.copy()
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error loading state: {e}")
            return DEFAULT_STATE.copy()


# Flask ওয়েব অ্যাপ্লিকেশন ইনিশিয়েট করা
app = Flask(__name__)


# =====================================================================
# SECTION 3: ১৭টি শক্তিশালী ক্যান্ডেলস্টিক প্যাটার্ন ডিটেক্টর
# =====================================================================
def get_advanced_pats(df):
    p = []
    if len(df) < 5:
        return p
    c1, c2, c3 = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    def info(c):
        body = abs(c['c'] - c['o'])
        total = max(0.001, c['h'] - c['l'])
        u_wick = c['h'] - max(c['c'], c['o'])
        l_wick = min(c['c'], c['o']) - c['l']
        is_green = c['c'] > c['o']
        return body, total, u_wick, l_wick, is_green
        
    b1, t1, u1, l1, g1 = info(c1)
    b2, t2, u2, l2, g2 = info(c2)
    b3, t3, u3, l3, g3 = info(c3)

    if b1 > 0 and l1 >= 1.8 * b1 and u1 <= 0.2 * b1: p.append({"n": "হ্যামার 🔨", "t": "bull"})
    if b1 > 0 and u1 >= 1.8 * b1 and l1 <= 0.2 * b1 and g1: p.append({"n": "ইনভার্টেড হ্যামার 🔨", "t": "bull"})
    if not g2 and g1 and c1['c'] >= c2['o'] and c1['o'] <= c2['c']: p.append({"n": "বুলিশ এনগালফিং 📈", "t": "bull"})
    if not g3 and b2 < (b3 * 0.3) and g1 and c1['c'] > (c3['o'] + c3['c']) / 2: p.append({"n": "মর্নিং স্টার 🌅", "t": "bull"})
    if b1 / t1 > 0.85 and g1: p.append({"n": "বুলিশ মারুবোজু 💪", "t": "bull"})
    if not g2 and g1 and c1['o'] < c2['c'] and c1['c'] > (c2['o'] + c2['c']) / 2 and c1['c'] < c2['o']: p.append({"n": "পিয়ার্সিং লাইন ⚡", "t": "bull"})
    if not g2 and g1 and c1['c'] < c2['o'] and c1['o'] > c2['c'] and b1 < b2: p.append({"n": "বুলিশ হারামি 🤰", "t": "bull"})
    if g1 and g2 and g3 and c1['c'] > c2['c'] and c2['c'] > c3['c'] and b1 > 0.3 * t1 and b2 > 0.3 * t2: p.append({"n": "থ্রি হোয়াইট সোলজার্স 💂‍♂️", "t": "bull"})
    if abs(c1['l'] - c2['l']) / max(0.001, c1['l']) < 0.001 and not g2 and g1: p.append({"n": "টুইজার বটম 🧲", "t": "bull"})

    if b1 > 0 and u1 >= 1.8 * b1 and l1 <= 0.2 * b1 and not g1: p.append({"n": "শুটিং স্টার ☄️", "t": "bear"})
    if b1 > 0 and l1 >= 1.8 * b1 and u1 <= 0.2 * b1 and not g1: p.append({"n": "হ্যাঙ্গিং ম্যান 🕴️", "t": "bear"})
    if g2 and not g1 and c1['c'] <= c2['o'] and c1['o'] >= c2['c']: p.append({"n": "বেয়ারিশ এনগালফিং 📉", "t": "bear"})
    if g3 and b2 < (b3 * 0.3) and not g1 and c1['c'] < (c3['o'] + c3['c']) / 2: p.append({"n": "ইভনিং স্টার 🌅", "t": "bear"})
    if b1 / t1 > 0.85 and not g1: p.append({"n": "বেয়ারিশ মারুবোজু 🔴", "t": "bear"})
    if g2 and not g1 and c1['o'] > c2['c'] and c1['c'] < (c2['o'] + c2['c']) / 2 and c1['c'] > c2['o']: p.append({"n": "ডার্ক ক্লাউড কভার ⛈️", "t": "bear"})
    if g2 and not g1 and c1['c'] > c2['o'] and c1['o'] < c2['c'] and b1 < b2: p.append({"n": "বেয়ারিশ হারামি 🤰", "t": "bear"})
    if not g1 and not g2 and not g3 and c1['c'] < c2['c'] and c2['c'] < c3['c'] and b1 > 0.3 * t1 and b2 > 0.3 * t2: p.append({"n": "থ্রি ব্ল্যাক ক্রোস 🐦", "t": "bear"})
    return p


# =====================================================================
# SECTION 4: সুইং ও ট্রেন্ড ট্রেডিং বট ইঞ্জিন (রিস্যাম্পলড ১৫মি ও ১ঘণ্টা চার্ট)
# =====================================================================
def bot_engine():
    wins, total, net_pnl, pnl_hist = 0, 0, 0.0, [0]
    in_pos, entry_p, peak_p = False, 0.0, 0.0
    
    last_trade_time = 0         
    COOLDOWN_SECONDS = 900      # ট্রেন্ড ক্লোজ হওয়ার পর ১৫ মিনিট (৯০০ সেকেন্ড) বিরতি

    while True:
        try:
            # ১৫ মিনিটের ক্যান্ডেলস্টিক ডাটা সংগ্রহ (৪০০টি ক্যান্ডেল, যা থেকে ১ ঘণ্টার চার্ট তৈরি হবে)
            bars15 = exchange.fetch_ohlcv(SYMBOL, '15m', limit=400)
            df15 = pd.DataFrame(bars15, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            
            # ডেটটাইম ইনডেক্স সেট করা (রিস্যাম্পলিংয়ের জন্য)
            df15['dt'] = pd.to_datetime(df15['t'], unit='ms')
            df15.set_index('dt', inplace=True)
            
            # ১৫-মিনিটের ডাটা জোড়া দিয়ে মেমোরিতে ১-ঘণ্টার (1H) চার্ট তৈরি করা (সম্পূর্ণ নো-ল্যাগ)
            df1h = df15.resample('1H').agg({
                't': 'first',
                'o': 'first',
                'h': 'max',
                'l': 'min',
                'c': 'last',
                'v': 'sum'
            }).dropna()
            df1h.reset_index(drop=True, inplace=True)
            df15.reset_index(drop=True, inplace=True)
            
            p = df15['c'].iloc[-1]
            
            # ১৫-মিনিট ইন্ডিকেটর (RSI 14, EMA 20, EMA 50)
            r15 = ta.momentum.rsi(df15['c'], window=14).fillna(0).iloc[-1]
            e20 = ta.trend.ema_indicator(df15['c'], 20).fillna(0).iloc[-1]
            e50 = ta.trend.ema_indicator(df15['c'], 50).fillna(0).iloc[-1]
            
            # ১-ঘণ্টা চার্ট ইন্ডিকেটর (RSI 14, EMA 200, MACD)
            r1h = ta.momentum.rsi(df1h['c'], window=14).fillna(0).iloc[-1]
            e200 = ta.trend.ema_indicator(df1h['c'], 200).fillna(0).iloc[-1]
            
            m_obj = ta.trend.MACD(df1h['c'])
            mv = m_obj.macd().iloc[-1]
            ms = m_obj.macd_signal().iloc[-1]
            
            pats15 = get_advanced_pats(df15)
            pats1h = get_advanced_pats(df1h)
            
            cur = load_state()
            
            # ওল্ড স্টেট ফাইল সেফলি মার্জ করা
            for k, v in DEFAULT_STATE.items():
                if k not in cur:
                    cur[k] = v
                    
            l_pnl = ((p / entry_p) - 1) * 100 if in_pos else 0.0
            l_val = (100.0 / entry_p * p) - 100.0 if in_pos else 0.0

            # কুলডাউন যাচাই
            time_since_last_trade = time.time() - last_trade_time
            cooldown_over = time_since_last_trade >= COOLDOWN_SECONDS

            # ১. ক্রয়ের লজিক (BUY Condition - সুইং ট্রেডিং উপযোগী)
            bull_signal = any(pt['t'] == 'bull' for pt in pats15) or any(pt['t'] == 'bull' for pt in pats1h)
            
            # শর্ত: ১ ঘণ্টা চার্ট ইএমএ ২০০ এর উপরে (ম্যাক্রো আপট্রেন্ড), ১৫ মিনিটে ইএমএ ২০ ও ৫০ এর উপরে, RSI ৪০-৬৫ এর মধ্যে, MACD বুলিশ, প্যাটার্ন ও কুলডাউন সম্পন্ন
            macro_bullish = p > e200
            ema_alignment = p > e20 and p > e50
            can_buy = (macro_bullish and ema_alignment and (40 < r15 < 65) and (mv > ms) and bull_signal and cooldown_over)

            # ২. বিক্রয়ের লজিক (SELL / Trend Reversal Exit)
            # শর্ত: ১৫-মিনিটে মূল্য ইএমএ ৫০ এর নিচে নামলে (ট্রেন্ড শেষ) অথবা RSI অতিরিক্ত ওভারবট (>৭৮) হলে প্রফিট বুক
            smart_sell = p < e50 or r15 > 78

            if in_pos:
                # ক. নো-লস সুরক্ষাকবচ (Breakeven Protection)
                # ট্রেন্ড ট্রেডিংয়ে মূল্য যখনই এন্ট্রি থেকে ১.২% ওপরে যাবে, স্টপ লস এন্ট্রিতে আসবে (ঝুঁকিমুক্ত ট্রেড)
                breakeven_trigger = entry_p * 1.012
                if p >= breakeven_trigger and cur["sl_level"] < entry_p:
                    cur.update({"sl_level": round(entry_p, 2)})
                    cur["log"].insert(0, {"t": datetime.now().strftime("%H:%M"), "m": "🛡️ SL Breakeven-এ উন্নীত (ঝুঁকিমুক্ত সুইং ট্রেড)"})

                # খ. ট্রেইলিং স্টপ লস আপডেট করা
                if p > peak_p:
                    peak_p = p
                    new_sl = round(p * (1 - DEF_SL), 2)  # ২% ট্রেইলিং স্টপ লস
                    if new_sl > cur["sl_level"]:
                        cur.update({"sl_level": new_sl})

                # গ. টেক প্রফিট (৩.৫%), স্টপ লস (২.০%) অথবা স্মার্ট সেল ট্রিগার হলে পজিশন ক্লোজ
                if p >= cur["tp_level"] or p <= cur["sl_level"] or smart_sell:
                    in_pos = False
                    net_pnl += l_val
                    pnl_hist.append(net_pnl)
                    
                    if p > entry_p: wins += 1
                        
                    cur.update({
                        "balance": round(100.0 + net_pnl, 2),
                        "total_pnl": round(net_pnl, 2),
                        "win_rate": round((wins / total) * 100, 1),
                        "best": round(max(pnl_hist), 2),
                        "last_action": "SELL"
                    })
                    cur["history"].insert(0, {"t": datetime.now().strftime("%H:%M"), "a": "SELL", "p": round(p, 2), "r": f"{round(l_pnl, 2)}%"})
                    cur["log"].insert(0, {"t": datetime.now().strftime("%H:%M"), "m": f"🔴 SELL @ ${p:.2f} ({'Smart Exit' if smart_sell else 'Target'})"})
                    
                    last_trade_time = time.time()
            else:
                if can_buy:
                    entry_p = p
                    peak_p = p
                    in_pos = True
                    total += 1
                    
                    cur.update({
                        "trades": total,
                        "balance": 0.0,
                        "sl_level": round(p * (1 - DEF_SL), 2),
                        "tp_level": round(p * (1 + DEF_TP), 2),
                        "last_action": "BUY"
                    })
                    cur["history"].insert(0, {"t": datetime.now().strftime("%H:%M"), "a": "BUY", "p": round(p, 2), "r": "---"})
                    cur["log"].insert(0, {"t": datetime.now().strftime("%H:%M"), "m": f"🟢 BUY @ ${p:.2f} (Trend Confirmed)"})
            
            # ড্যাশবোর্ডের জন্য স্টেট আপডেট
            cur.update({
                "price": round(p, 2),
                "last_update": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "in_position": in_pos,
                "live_pnl_pct": round(l_pnl, 2),
                "live_pnl_val": round(l_val, 2),
                "entry_price": round(entry_p, 2),
                "analysis_15m": {
                    "rsi": round(r15, 1),
                    "ema20": round(e20, 2),
                    "ema50": round(e50, 2),
                    "sig": "বুলিশ ✅" if p > e20 else "বেয়ারিশ ❌",
                    "pats": pats15
                },
                "analysis_1h": {
                    "rsi": round(r1h, 1),
                    "ema200": round(e200, 2),
                    "sig": "বুলিশ ✅" if p > e200 else "বেয়ারিশ ❌",
                    "pats": pats1h
                }
            })
            
            # ড্যাশবোর্ডের জন্য ওয়েটিং মেসেজ সাজানো
            if in_pos:
                cur["wait_reason"] = "পজিশন সক্রিয়"
            elif not cooldown_over:
                remaining_seconds = int(COOLDOWN_SECONDS - time_since_last_trade)
                cur["wait_reason"] = f"কুলডাউন ({int(remaining_seconds/60)} মিনিট বাকি)"
            elif not macro_bullish:
                cur["wait_reason"] = "১-ঘণ্টা চার্ট বেয়ারিশ (মূল্য EMA 200 এর নিচে)"
            elif not ema_alignment:
                cur["wait_reason"] = "১৫-মিনিট চার্টে রিট্রেসমেন্ট চলছে"
            else:
                cur["wait_reason"] = "সুইং এন্ট্রি প্যাটার্ন খুঁজছে..."
                
            save_state(cur)
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Bot Engine Warning: {e}")
            
        # সুইং ট্রেডিংয়ের জন্য ১০ সেকেন্ড লোড অত্যন্ত নিরাপদ ও লাইটওয়েট
        time.sleep(10)


# ব্যাকগ্রাউন্ড ট্রেডিং থ্রেড রান করা
threading.Thread(target=bot_engine, daemon=True).start()


# =====================================================================
# SECTION 5: Flask ওয়েব সার্ভার এবং এপিআই রাউটস (High Performance)
# =====================================================================
@app.route('/api/data')
def api():
    """মেমোরি ক্যাশ থেকে ইনস্ট্যান্ট ডাটা ড্যাশবোর্ডে পাঠায় (০ মিলি-সেকেন্ড ল্যাগ)"""
    return jsonify(load_state())


@app.route('/')
def index():
    """ওয়েব ড্যাশবোর্ড লোড করে"""
    return render_template_string(UI)


# =====================================================================
# SECTION 6: ড্যাশবোর্ড UI টেমপ্লেট (১৫মিনিট ও ১ঘণ্টা উপযোগী)
# =====================================================================
UI = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Master SOL Bot</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>setInterval(() => location.reload(), 600000);</script>
    <style>
        body { background-color: #f8fafc; font-family: 'Segoe UI', sans-serif; }
        .card { background: white; border-radius: 1rem; border: 1px solid #f1f5f9; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); }
        .tag { border: 1px solid #dcfce7; color: #166534; padding: 2px 10px; border-radius: 99px; font-size: 10px; font-weight: 800; display: inline-block; margin: 2px; }
        .tag-bull { background: #f0fdf4; } 
        .tag-bear { background: #fef2f2; color: #991b1b; border-color: #fee2e2; }
    </style>
</head>
<body class="p-3 text-slate-800">
<div class="max-w-md mx-auto">
    <!-- বট স্ট্যাটাস -->
    <div class="text-center mb-6">
        <span class="bg-green-100 text-green-700 px-4 py-1 rounded-lg text-xs font-bold border border-green-200">&#9989; বট চলছে</span>
    </div>
    
    <!-- পরিসংখ্যান -->
    <div class="grid grid-cols-3 gap-2 mb-2 text-center text-[10px] font-bold text-slate-400 uppercase">
        <div class="card p-3"><p>মোট ট্রেড</p><p id="t" class="text-lg font-black text-slate-800">0</p></div>
        <div class="card p-3"><p>জয়ের হার</p><p id="w" class="text-lg font-black text-slate-800">0%</p></div>
        <div class="card p-3"><p>মোট P&L</p><p id="pnl" class="text-lg font-black text-green-600">+$0.00</p></div>
    </div>

    <div class="grid grid-cols-3 gap-2 mb-4 text-center text-[9px] font-bold text-slate-400 uppercase">
        <div class="card p-3"><p>সেরা</p><p id="bt" class="text-xs font-bold text-green-400">--</p></div>
        <div class="card p-3"><p>খারাপ</p><p id="wt" class="text-xs font-bold text-red-400">--</p></div>
        <div class="card p-3"><p>শেষ</p><p id="la" class="text-xs font-bold text-slate-500">---</p></div>
    </div>

    <!-- রিয়েল-টাইম মূল্য ও ব্যালেন্স কার্ড -->
    <div class="card p-6 mb-4 text-center">
        <div class="flex justify-between items-center mb-4">
            <span id="pr" class="text-4xl font-black tracking-tighter">$0.00</span>
            <div class="text-right text-[10px] text-slate-400 font-bold">ব্যালেন্স: <b id="bl">$100.00</b></div>
        </div>
        
        <!-- লাইভ পজিশন কালার বক্স -->
        <div id="pnl_display" class="hidden mb-4 p-5 border-2 rounded-3xl text-center bg-white shadow-lg">
            <p class="text-[10px] font-bold text-slate-400 uppercase mb-1">লাইভ পজিশন প্রফিট</p>
            <p id="lp" class="text-4xl font-black">0.00%</p>
            <div class="flex justify-around mt-4 text-[10px] font-bold border-t pt-2">
                <div class="text-red-500">🛑 SL: <span id="sl">0</span></div>
                <div class="text-green-600">✅ TP: <span id="tp">0</span></div>
            </div>
        </div>
        
        <div id="st" class="bg-orange-50 text-orange-600 p-2.5 rounded-xl text-[11px] font-bold border border-orange-100 text-center uppercase tracking-wide italic">&#8987; লোড হচ্ছে...</div>
    </div>

    <!-- ১৫ মিনিট বিশ্লেষণ প্যানেল -->
    <div class="card p-4 mb-4 text-[11px]">
        <div class="flex justify-between mb-3 items-center">
            <h3 class="font-bold text-slate-700 text-xs">&#128202; ১৫ মিনিট বিশ্লেষণ</h3>
            <span id="s15" class="font-bold px-2 py-0.5 rounded text-[10px]">WAIT</span>
        </div>
        <div class="grid grid-cols-2 text-slate-500 font-medium">
            <span>RSI: <b id="r15">0</b></span>
            <span>EMA 20: <b id="e20">0</b></span>
            <span>EMA 50: <b id="e50">0</b></span>
        </div>
        <div id="pats15" class="mt-3 flex flex-wrap gap-1"></div>
    </div>
    
    <!-- ১ ঘণ্টা বিশ্লেষণ প্যানেল -->
    <div class="card p-4 mb-4 text-[11px]">
        <div class="flex justify-between mb-3 items-center">
            <h3 class="font-bold text-slate-700 text-xs">&#128202; ১ ঘণ্টা বিশ্লেষণ</h3>
            <span id="s1h" class="font-bold px-2 py-0.5 rounded text-[10px]">WAIT</span>
        </div>
        <div class="grid grid-cols-2 text-slate-500 font-medium">
            <span>RSI: <b id="r1h">0</b></span>
            <span>EMA 200: <b id="e200">0</b></span>
        </div>
        <div id="pats1h" class="mt-3 flex flex-wrap gap-1"></div>
    </div>

    <!-- চার্ট উইজেট (TradingView চার্টটিকে ১৫-মিনিটে আপডেট করা হয়েছে) -->
    <div class="card overflow-hidden h-60 mb-4 border border-slate-100 shadow-inner">
        <iframe src="https://s.tradingview.com/widgetembed/?symbol=BITGET%3ASOLUSDT&interval=15&theme=light" width="100%" height="100%" frameborder="0"></iframe>
    </div>
    
    <!-- ট্রেড হিস্ট্রি টেবিল -->
    <div class="card p-4 mb-4 overflow-hidden">
        <h3 class="font-black text-slate-700 text-[10px] mb-3 uppercase tracking-wider">&#128203; ট্রেড হিস্ট্রি</h3>
        <div class="overflow-x-auto">
            <table class="w-full text-[10px] text-left">
                <thead class="text-slate-400 border-b">
                    <tr>
                        <th class="pb-2">সময়</th>
                        <th class="pb-2 text-center">ধরন</th>
                        <th class="pb-2 text-right">মূল্য</th>
                        <th class="pb-2 text-right">P&L</th>
                    </tr>
                </thead>
                <tbody id="hb" class="divide-y divide-slate-50"></tbody>
            </table>
        </div>
    </div>
    
    <!-- লাইভ লগ প্যানেল -->
    <div class="card p-4 mb-6">
        <h3 class="font-bold text-slate-700 text-xs mb-2 uppercase tracking-widest">&#128214; লাইভ লগ</h3>
        <div id="lg" class="space-y-1 text-[10px]"></div>
    </div>
</div>

<script>
    async function update() {
        try {
            const r = await fetch('/api/data'); 
            const d = await r.json();
            
            if (d.price > 0) {
                // মূল ব্যালেন্স ও রিয়েল-টাইম প্রাইস আপডেট
                document.getElementById('pr').innerText = '$' + d.price; 
                document.getElementById('bl').innerText = '$' + d.balance.toFixed(2);
                
                // পরিসংখ্যান আপডেট
                document.getElementById('t').innerText = d.trades; 
                document.getElementById('w').innerText = d.win_rate + '%';
                document.getElementById('pnl').innerText = (d.total_pnl >= 0 ? '+$' : '$') + d.total_pnl.toFixed(2);
                document.getElementById('bt').innerText = '$' + d.best.toFixed(2); 
                document.getElementById('wt').innerText = '$' + d.worst.toFixed(2);
                document.getElementById('la').innerText = d.last_action; 
                document.getElementById('st').innerText = '⌛ ' + d.wait_reason;
                
                // লাইভ ট্রেড ওপেন থাকলে প্রফিট-বক্স শো করা
                if (d.in_position) {
                    const disp = document.getElementById('pnl_display'); 
                    disp.classList.remove('hidden');
                    
                    document.getElementById('lp').innerText = (d.live_pnl_pct >= 0 ? '+' : '') + d.live_pnl_pct + '%';
                    document.getElementById('sl').innerText = d.sl_level; 
                    document.getElementById('tp').innerText = d.tp_level;
                    
                    const col = d.live_pnl_pct >= 0 ? 'text-green-600' : 'text-red-600';
                    document.getElementById('lp').className = 'text-4xl font-black ' + col;
                    disp.className = 'mb-4 p-5 border-2 rounded-3xl text-center bg-white shadow-lg ' + (d.live_pnl_pct >= 0 ? 'border-green-100' : 'border-red-100');
                } else { 
                    document.getElementById('pnl_display').classList.add('hidden'); 
                }
                
                // ১৫ মিনিট সিগন্যাল ডাইনামিক স্টাইল
                document.getElementById('r15').innerText = d.analysis_15m.rsi; 
                document.getElementById('e20').innerText = '$' + d.analysis_15m.ema20;
                document.getElementById('e50').innerText = '$' + d.analysis_15m.ema50;
                
                const s15 = document.getElementById('s15'); 
                s15.innerText = d.analysis_15m.sig;
                if (d.analysis_15m.sig.includes('বুলিশ')) {
                    s15.className = 'font-bold px-2 py-0.5 rounded text-[10px] bg-green-50 text-green-700 border border-green-200';
                } else if (d.analysis_15m.sig.includes('বেয়ারিশ')) {
                    s15.className = 'font-bold px-2 py-0.5 rounded text-[10px] bg-red-50 text-red-700 border border-red-200';
                } else {
                    s15.className = 'font-bold px-2 py-0.5 rounded text-[10px] bg-slate-100 text-slate-600 border border-slate-200';
                }
                
                // ১ ঘণ্টা সিগন্যাল ডাইনামিক স্টাইল
                document.getElementById('r1h').innerText = d.analysis_1h.rsi; 
                document.getElementById('e200').innerText = '$' + d.analysis_1h.ema200;
                
                const s1h = document.getElementById('s1h'); 
                s1h.innerText = d.analysis_1h.sig;
                if (d.analysis_1h.sig.includes('বুলিশ')) {
                    s1h.className = 'font-bold px-2 py-0.5 rounded text-[10px] bg-green-50 text-green-700 border border-green-200';
                } else if (d.analysis_1h.sig.includes('বেয়ারিশ')) {
                    s1h.className = 'font-bold px-2 py-0.5 rounded text-[10px] bg-red-50 text-red-700 border border-red-200';
                } else {
                    s1h.className = 'font-bold px-2 py-0.5 rounded text-[10px] bg-slate-100 text-slate-600 border border-slate-200';
                }

                // প্যাটার্ন ডিসপ্লে রেন্ডার
                const tag = (p) => `<span class="tag ${p.t==='bull'?'tag-bull':'tag-bear'}">${p.n}</span>`;
                const no_pat = '<p class="text-gray-400 italic text-[10px]">কোনো ক্যান্ডেলস্টিক প্যাটার্ন নেই</p>';
                
                document.getElementById('pats15').innerHTML = d.analysis_15m.pats.length > 0 ? d.analysis_15m.pats.map(tag).join('') : no_pat;
                document.getElementById('pats1h').innerHTML = d.analysis_1h.pats.length > 0 ? d.analysis_1h.pats.map(tag).join('') : no_pat;

                // ট্রেড হিস্ট্রি ডাটা টেবিল আপডেট
                document.getElementById('hb').innerHTML = d.history.slice(0,5).map(h => `
                    <tr class="border-b border-slate-50">
                        <td class="py-2 text-slate-400 font-bold">${h.t}</td>
                        <td class="font-black text-center ${h.a=='BUY'?'text-blue-500':'text-orange-500'}">${h.a}</td>
                        <td class="text-right font-black">$${h.p}</td>
                        <td class="text-right font-black ${h.r.includes('-')?'text-red-400':'text-green-500'}">${h.r}</td>
                    </tr>
                `).join('');
                
                // লাইভ লগ মেসেজ আপডেট
                document.getElementById('lg').innerHTML = d.log.slice(0,3).map(l => `
                    <div class="flex justify-between text-slate-500 pb-1">
                        <span>${l.t}</span>
                        <span>${l.m}</span>
                    </div>
                `).join('');
            }
        } catch (e) {}
    }
    // সুইং ট্রেডিংয়ের জন্য ৫ সেকেন্ড পর পর ড্যাশবোর্ড আপডেট হওয়াই যথেষ্ট
    setInterval(update, 5000); 
    update();
</script>
</body>
</html>
"""


# =====================================================================
# SECTION 7: অ্যাপ্লিকেশন এক্সিকিউশন ব্লক (Run App)
# =====================================================================
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
