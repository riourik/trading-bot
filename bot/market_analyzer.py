"""
Analyse de marché : fetch yfinance, calcul indicateurs techniques,
détection du régime marché (bull/bear/neutral), scoring des stocks.
"""
import numpy as np
import yfinance as yf
from datetime import datetime
from bot.logger import get_logger
import config

log = get_logger(__name__)

# Stocks prioritaires pour le bot (US + CA les plus liquides)
PRIORITY_US = [
    # Méga-cap US (core)
    "AAPL","MSFT","NVDA","GOOGL","META","AMZN","TSLA","AMD","PLTR","ARM",
    # Crypto / Finance haute volatilité
    "COIN","MSTR","MARA","RIOT","CLSK","HOOD","SOFI","AFRM","UPST",
    # Cybersec / Cloud / SaaS
    "CRWD","DDOG","NET","SNOW","PANW","ZS","OKTA","S","MDB","CFLT",
    "HUBS","MNDY","PATH","GTLB","SOUN","IONQ","AI",
    # Finance traditionnelle
    "JPM","BAC","GS","V","MA","XYZ","PYPL","CME","NDAQ",
    # Santé / Biotech
    "LLY","UNH","ABBV","AMGN","VRTX","ISRG","CRSP","HIMS","ILMN","RXRX",
    # Énergie
    "XOM","CVX","COP","OXY","ENPH","FSLR","PLUG",
    # Consumer / Retail / Loisirs
    "HD","COST","NFLX","UBER","LYFT","DASH","DKNG","LULU","CELH","MNST",
    "TGT","ROST","BKNG","ABNB","SBUX","QSR",
    # Communication / Social
    "RDDT","SNAP","PINS","ROKU","SPOT","RBLX","TTWO","EA",
    # EV
    "RIVN","NIO","XPEV",
    # Industrie / Défense / Espace
    "GE","CAT","BA","LMT","NOC","AXON","RKLB","BAH",
    # ETFs (utiles pour bench + position refuge)
    "SPY","QQQ","IWM","GLD","XLF","XLK","XLE","SOXX","XBI","ARKK",
]
PRIORITY_CA = [
    # Finance
    "RY.TO","TD.TO","BNS.TO","CM.TO","NA.TO","BAM.TO","BN.TO",
    "MFC.TO","FFH.TO","GWO.TO",
    # Tech
    "SHOP.TO","CSU.TO","TRI.TO","KXS.TO","LSPD.TO",
    # Énergie
    "CNQ.TO","SU.TO","ENB.TO","CVE.TO","IMO.TO","ARX.TO","PPL.TO",
    # Mines / Or
    "ABX.TO","WPM.TO","AEM.TO","CCO.TO","IVN.TO","LUN.TO","K.TO",
    # Tech
    "TOI.TO",
    # Crypto CA/US
    "BITF","HUT.TO","GLXY.TO",
    # Transport / Industrie
    "CNR.TO","CP.TO","WSP.TO","MG.TO","TFII.TO","WCN.TO",
    # Commerce / Divers
    "ATD.TO","DOL.TO","L.TO","MRU.TO",
]
PRIORITY_TICKERS = PRIORITY_US + PRIORITY_CA

# ── Indicateurs techniques ────────────────────────────────────────────────────

def _sma(arr: np.ndarray, period: int) -> float | None:
    if len(arr) < period:
        return None
    return float(np.mean(arr[-period:]))


def _ema(arr: np.ndarray, period: int) -> float:
    k = 2.0 / (period + 1)
    val = float(arr[0])
    for x in arr[1:]:
        val = float(x) * k + val * (1 - k)
    return val


def _rsi(closes: np.ndarray, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes.astype(float))
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _macd(closes: np.ndarray) -> tuple[float, float, float] | None:
    """Retourne (macd_line, signal_line, histogram)."""
    if len(closes) < 35:
        return None
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_line = ema12 - ema26
    # Approximation signal sur les 9 dernières valeurs MACD
    macd_series = np.array([
        _ema(closes[:i], 12) - _ema(closes[:i], 26)
        for i in range(26, len(closes) + 1)
    ])
    signal = _ema(macd_series, 9) if len(macd_series) >= 9 else macd_line
    return round(macd_line, 4), round(signal, 4), round(macd_line - signal, 4)


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    tr = []
    for i in range(1, len(closes)):
        tr.append(max(
            float(highs[i]) - float(lows[i]),
            abs(float(highs[i]) - float(closes[i - 1])),
            abs(float(lows[i])  - float(closes[i - 1])),
        ))
    return round(float(np.mean(tr[-period:])), 4)


def _bb_pct(closes: np.ndarray, period: int = 20) -> float | None:
    """Position du prix dans les Bollinger Bands (0=bas, 1=haut)."""
    if len(closes) < period:
        return None
    window = closes[-period:].astype(float)
    mid   = np.mean(window)
    std   = np.std(window)
    upper = mid + 2 * std
    lower = mid - 2 * std
    price = float(closes[-1])
    band_width = upper - lower
    if band_width == 0:
        return 0.5
    return round((price - lower) / band_width, 4)


# ── Fetch et calcul pour un ticker ───────────────────────────────────────────

def analyze_ticker(ticker: str, period: str = "6mo") -> dict | None:
    """Fetch yfinance + calcul complet des indicateurs. Retourne None si erreur."""
    try:
        df = yf.Ticker(ticker).history(period=period, interval="1d")
        if df.empty or len(df) < 30:
            return None

        closes = df["Close"].values
        highs  = df["High"].values
        lows   = df["Low"].values
        vols   = df["Volume"].values

        price   = round(float(closes[-1]), 2)
        prev    = float(closes[-2]) if len(closes) > 1 else price
        change  = round((price - prev) / prev * 100, 2) if prev else 0.0

        sma20  = _sma(closes, 20)
        sma50  = _sma(closes, 50)
        sma200 = _sma(closes, 200)
        rsi14  = _rsi(closes, 14)
        atr14  = _atr(highs, lows, closes, 14)
        bb     = _bb_pct(closes, 20)
        macd_r = _macd(closes)

        # Volume moyen 20j vs volume aujourd'hui
        avg_vol = float(np.mean(vols[-20:])) if len(vols) >= 20 else None
        vol_ratio = round(float(vols[-1]) / avg_vol, 2) if avg_vol and avg_vol > 0 else None

        # Score technique simple (0-100)
        score = _compute_score(price, rsi14, sma20, sma50, sma200, macd_r, bb, vol_ratio)

        return {
            "ticker":    ticker,
            "price":     price,
            "change":    change,
            "rsi":       rsi14,
            "sma20":     round(sma20, 2) if sma20 else None,
            "sma50":     round(sma50, 2) if sma50 else None,
            "sma200":    round(sma200, 2) if sma200 else None,
            "above_sma20":  price > sma20  if sma20  else None,
            "above_sma50":  price > sma50  if sma50  else None,
            "above_sma200": price > sma200 if sma200 else None,
            "macd":      macd_r[0] if macd_r else None,
            "macd_signal": macd_r[1] if macd_r else None,
            "macd_hist": macd_r[2] if macd_r else None,
            "atr":       atr14,
            "atr_pct":   round(atr14 / price * 100, 2) if atr14 else None,
            "bb_pct":    bb,
            "vol_ratio": vol_ratio,
            "score":     score,
        }
    except Exception as e:
        log.debug(f"analyze_ticker({ticker}) erreur: {e}")
        return None


def _compute_score(price, rsi, sma20, sma50, sma200, macd_r, bb, vol_ratio) -> float:
    """Score de 0 à 100 : plus élevé = opportunité d'achat plus forte."""
    pts = 50.0  # Neutre de base

    # RSI : oversold = opportunité, overbought = risque
    if rsi is not None:
        if rsi < 30:
            pts += 15
        elif rsi < 45:
            pts += 8
        elif rsi > 75:
            pts -= 15
        elif rsi > 60:
            pts -= 5

    # Tendance des SMA
    if sma20 and price > sma20:
        pts += 5
    if sma50 and price > sma50:
        pts += 8
    if sma200 and price > sma200:
        pts += 10

    # MACD : histogramme positif = momentum haussier
    if macd_r:
        hist = macd_r[2]
        if hist > 0:
            pts += 7
        else:
            pts -= 5

    # Bollinger Bands : bas de bande = support, haut = résistance
    if bb is not None:
        if bb < 0.2:
            pts += 10   # Prix près du bas de bande
        elif bb > 0.8:
            pts -= 10   # Prix près du haut, risque de retournement

    # Volume élevé = confirmation du mouvement
    if vol_ratio and vol_ratio > 1.5:
        pts += 5

    return round(max(0, min(100, pts)), 1)


# ── Régime de marché ──────────────────────────────────────────────────────────

def get_market_regime() -> dict:
    """
    Analyse SPY, VIX, TSX, Gold, DXY pour déterminer le régime.
    Retourne un dict avec regime ('bull'|'bear'|'neutral') et métriques.
    """
    log.info("Analyse du régime de marché...")
    metrics = {}

    tickers_to_fetch = {
        "SPY":     "S&P 500",
        "^VIX":    "VIX",
        "^GSPTSE": "TSX",
        "GC=F":    "Gold",
        "DX-Y.NYB":"DXY",
        "QQQ":     "NASDAQ ETF",
    }

    raw = {}
    for sym, name in tickers_to_fetch.items():
        try:
            df = yf.Ticker(sym).history(period="1y", interval="1d")
            if not df.empty:
                raw[sym] = df
        except Exception:
            pass

    # SPY
    spy_data = raw.get("SPY")
    if spy_data is not None and len(spy_data) >= 200:
        closes = spy_data["Close"].values
        spy_price  = float(closes[-1])
        spy_change = round((spy_price - float(closes[-2])) / float(closes[-2]) * 100, 2)
        sma200 = float(np.mean(closes[-200:]))
        sma50  = float(np.mean(closes[-50:]))
        sma20  = float(np.mean(closes[-20:]))
        rsi14  = _rsi(closes, 14)
        metrics["spy_price"]   = round(spy_price, 2)
        metrics["spy_change"]  = spy_change
        metrics["spy_sma20"]   = round(sma20, 2)
        metrics["spy_sma50"]   = round(sma50, 2)
        metrics["spy_sma200"]  = round(sma200, 2)
        metrics["spy_above_200"] = spy_price > sma200
        metrics["spy_above_50"]  = spy_price > sma50
        metrics["spy_rsi"]     = rsi14
        # Momentum : SPY > SMA20 > SMA50 = fort uptrend
        metrics["spy_uptrend"] = (spy_price > sma20 and sma20 > sma50)
    else:
        metrics["spy_above_200"] = True  # Défaut prudent

    # VIX
    vix_data = raw.get("^VIX")
    if vix_data is not None and len(vix_data) > 0:
        vix = float(vix_data["Close"].values[-1])
        metrics["vix"] = round(vix, 2)
        if vix < 15:
            metrics["vix_regime"] = "calme"
        elif vix < 20:
            metrics["vix_regime"] = "normal"
        elif vix < 30:
            metrics["vix_regime"] = "anxieux"
        elif vix < 40:
            metrics["vix_regime"] = "peur"
        else:
            metrics["vix_regime"] = "panique"

    # TSX
    tsx_data = raw.get("^GSPTSE")
    if tsx_data is not None and len(tsx_data) >= 50:
        closes = tsx_data["Close"].values
        tsx_price = float(closes[-1])
        sma50_tsx = float(np.mean(closes[-50:]))
        metrics["tsx_price"]     = round(tsx_price, 2)
        metrics["tsx_above_50"]  = tsx_price > sma50_tsx
        metrics["tsx_change"]    = round((tsx_price - float(closes[-2])) / float(closes[-2]) * 100, 2)

    # Gold
    gold_data = raw.get("GC=F")
    if gold_data is not None and len(gold_data) >= 20:
        closes = gold_data["Close"].values
        gold_price = float(closes[-1])
        gold_sma20 = float(np.mean(closes[-20:]))
        metrics["gold_price"]    = round(gold_price, 2)
        metrics["gold_change"]   = round((gold_price - float(closes[-2])) / float(closes[-2]) * 100, 2)
        metrics["gold_rising"]   = gold_price > gold_sma20  # Gold rise = risk-off signal

    # DXY
    dxy_data = raw.get("DX-Y.NYB")
    if dxy_data is not None and len(dxy_data) >= 20:
        closes = dxy_data["Close"].values
        metrics["dxy"] = round(float(closes[-1]), 2)
        metrics["dxy_change"] = round((float(closes[-1]) - float(closes[-2])) / float(closes[-2]) * 100, 2)

    # ── Décision régime ───────────────────────────────────────────────────────
    regime = _decide_regime(metrics)
    metrics["regime"] = regime
    log.info(f"Régime détecté: {regime.upper()} | VIX={metrics.get('vix','?')} | SPY>200SMA={metrics.get('spy_above_200','?')}")
    return metrics


def _decide_regime(m: dict) -> str:
    """Logique de décision du régime basée sur les indicateurs."""
    bull_points = 0
    bear_points = 0

    # SPY vs SMA200 (signal le plus important)
    if m.get("spy_above_200"):
        bull_points += 3
    else:
        bear_points += 3

    # SPY vs SMA50
    if m.get("spy_above_50"):
        bull_points += 2
    else:
        bear_points += 2

    # SPY momentum
    if m.get("spy_uptrend"):
        bull_points += 2
    else:
        bear_points += 1

    # VIX
    vix = m.get("vix", 20)
    if vix < 15:
        bull_points += 3
    elif vix < 20:
        bull_points += 1
    elif vix > 30:
        bear_points += 3
    elif vix > 25:
        bear_points += 2

    # Gold rising = risk-off
    if m.get("gold_rising"):
        bear_points += 1

    # TSX
    if m.get("tsx_above_50"):
        bull_points += 1
    else:
        bear_points += 1

    # SPY RSI extrêmes
    rsi = m.get("spy_rsi")
    if rsi:
        if rsi < 30:
            bear_points += 2  # Oversold mais signal bear
        elif rsi > 70:
            bull_points += 1

    if bull_points > bear_points + 2:
        return "bull"
    elif bear_points > bull_points + 2:
        return "bear"
    return "neutral"


# ── Sélection des meilleurs candidats ────────────────────────────────────────

def get_top_candidates(n: int = 20, regime: str = "neutral") -> list[dict]:
    """
    Analyse les stocks prioritaires et retourne les N meilleurs candidats
    avec leurs indicateurs techniques.
    """
    log.info(f"Analyse de {len(PRIORITY_TICKERS)} stocks candidats...")

    # En mode bear, on favorise les valeurs défensives (CAC DE BASE, Gold miners)
    if regime == "bear":
        defensive = [
            "GLD","WMT","KO","PG","JNJ","PEP","MO","NEE","DUK",
            "GC=F","ABX.TO","WPM.TO","AEM.TO","GOLD","WPM","AEM",
        ]
        tickers = defensive + [t for t in PRIORITY_TICKERS if t not in defensive]
    else:
        tickers = PRIORITY_TICKERS

    results = []
    for ticker in tickers:
        data = analyze_ticker(ticker)
        if data and data["price"] > 0:
            results.append(data)

    # Trier par score décroissant
    results.sort(key=lambda x: x.get("score", 0), reverse=True)

    log.info(f"Top 5 scores: {[(r['ticker'], r['score']) for r in results[:5]]}")
    return results[:n]
