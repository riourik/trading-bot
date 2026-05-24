"""
Boucle principale du trading bot.
Scheduler APScheduler : analyse toutes les heures pendant les heures de marché.
Gestion complète du cycle buy/sell + stop loss + take profit.
"""
import time
import pytz
from datetime import datetime, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot.api_client import FinanceAcademyClient
from bot.market_analyzer import get_market_regime, get_top_candidates, analyze_ticker, get_news_for_tickers
from bot.llm_agent import LLMAgent
from bot.risk_manager import RiskManager
from bot.logger import get_logger
import config

log = get_logger("main")
EST = pytz.timezone(config.MARKET_TIMEZONE)


def is_market_open(now: datetime | None = None) -> bool:
    """Vérifie si le marché US/CA est ouvert (L-V 9h30-16h00 EST)."""
    if now is None:
        now = datetime.now(EST)
    if now.weekday() >= 5:  # Samedi=5, Dimanche=6
        return False
    market_open  = now.replace(hour=config.MARKET_OPEN_HOUR,  minute=config.MARKET_OPEN_MIN,  second=0, microsecond=0)
    market_close = now.replace(hour=config.MARKET_CLOSE_HOUR, minute=config.MARKET_CLOSE_MIN, second=0, microsecond=0)
    return market_open <= now <= market_close


def minutes_to_open() -> int:
    """Minutes avant l'ouverture du marché."""
    now = datetime.now(EST)
    if now.weekday() >= 5:
        days_until_monday = (7 - now.weekday()) % 7
        next_open = (now + timedelta(days=days_until_monday)).replace(
            hour=config.MARKET_OPEN_HOUR, minute=config.MARKET_OPEN_MIN, second=0
        )
    else:
        today_open = now.replace(hour=config.MARKET_OPEN_HOUR, minute=config.MARKET_OPEN_MIN, second=0)
        if now < today_open:
            next_open = today_open
        else:
            days = 1 if now.weekday() < 4 else (7 - now.weekday())
            next_open = (now + timedelta(days=days)).replace(
                hour=config.MARKET_OPEN_HOUR, minute=config.MARKET_OPEN_MIN, second=0
            )
    return max(0, int((next_open - now).total_seconds() / 60))


# ── Composants globaux ────────────────────────────────────────────────────────

client  = FinanceAcademyClient()
agent   = LLMAgent()
risk_mgr = RiskManager()


# ── Cycle principal de trading ────────────────────────────────────────────────

def run_trading_cycle():
    """
    Cycle complet exécuté toutes les heures pendant les heures de marché:
    1. Fetch portfolio + marché
    2. Vérifications de sécurité (stop d'urgence, perte journalière)
    3. Analyse technique des candidats
    4. Décision LLM
    5. Exécution des trades
    """
    now = datetime.now(EST)
    log.info(f"{'='*60}")
    log.info(f"CYCLE {now.strftime('%Y-%m-%d %H:%M')} EST")
    log.info(f"{'='*60}")

    # 1. Portfolio actuel
    try:
        portfolio = client.get_portfolio()
    except Exception as e:
        log.error(f"Impossible de récupérer le portfolio: {e}")
        return

    total_value = portfolio.get("portfolio_value", portfolio.get("cash", 0))

    # 2. Vérification perte journalière
    if not risk_mgr.check_daily_loss_limit(total_value):
        log.warning("Trading suspendu pour la journée (limite de perte atteinte)")
        return

    # 3. Vérifications de sortie d'urgence (avant toute décision LLM)
    emergency_exits = risk_mgr.check_positions_for_exit(portfolio)
    for exit_signal in emergency_exits:
        ticker = exit_signal["ticker"]
        reason = exit_signal["reason"]
        log.warning(f"EXIT D'URGENCE {ticker}: {reason}")
        ok, msg = risk_mgr.can_sell(ticker, exit_signal["quantity"], portfolio)
        if ok:
            client.sell(ticker)
            log.info(f"Position {ticker} fermée en urgence")

    # 4. Régime de marché
    try:
        market = get_market_regime()
    except Exception as e:
        log.error(f"Erreur analyse marché: {e}")
        market = {"regime": "neutral"}

    regime = market.get("regime", "neutral")
    log.info(risk_mgr.get_portfolio_summary(portfolio, regime))

    # 5. LLM disponible ?
    if not agent.is_available():
        log.warning("LM Studio inaccessible — cycle annulé")
        log.warning(f"Vérifie que LM Studio tourne sur {config.LLM_BASE_URL}")
        return

    # 6. Analyse des candidats
    try:
        candidates = get_top_candidates(n=config.TOP_STOCKS_COUNT, regime=regime)
    except Exception as e:
        log.error(f"Erreur analyse candidats: {e}")
        candidates = []

    if not candidates:
        log.warning("Aucun candidat disponible, cycle annulé")
        return

    # 7. News récentes pour les top candidats + positions ouvertes
    try:
        top_tickers    = [c["ticker"] for c in candidates[:15]]
        held_tickers   = [p["ticker"] for p in portfolio.get("positions", [])]
        all_news_tickers = list(dict.fromkeys(held_tickers + top_tickers))  # positions en priorité
        news = get_news_for_tickers(all_news_tickers, max_per_ticker=3)
        log.info(f"News récupérées pour {len(news)}/{len(all_news_tickers)} tickers (positions + candidats)")
    except Exception as e:
        log.warning(f"Impossible de récupérer les news: {e}")
        news = {}

    # 8. Décision LLM
    try:
        decision = agent.decide(portfolio, market, candidates, news=news)
    except Exception as e:
        log.error(f"Erreur agent LLM: {e}")
        return

    if not decision:
        log.warning("LLM n'a pas retourné de décision valide")
        return

    # 9. Exécution des actions
    actions = decision.get("actions", [])
    log.info(f"LLM propose {len(actions)} action(s)")

    executed_buys  = 0
    executed_sells = 0

    for act in actions:
        action = act["action"]
        ticker = act["ticker"]

        if action == "hold":
            log.info(f"HOLD {ticker}: {act.get('reasoning', '')}")
            continue

        elif action == "sell":
            ok, msg = risk_mgr.can_sell(ticker, 0, portfolio)
            if not ok:
                log.warning(f"SELL {ticker} refusé: {msg}")
                continue
            try:
                client.sell(ticker)
                executed_sells += 1
                log.info(f"SELL {ticker} exécuté | {act.get('reasoning', '')}")
            except Exception as e:
                log.error(f"Erreur SELL {ticker}: {e}")

        elif action == "buy":
            # Récupérer le prix actuel
            quote = client.get_quote(ticker)
            if not quote or quote.get("price", 0) <= 0:
                # Fallback via yfinance
                ticker_data = analyze_ticker(ticker, period="5d")
                if not ticker_data or ticker_data["price"] <= 0:
                    log.warning(f"BUY {ticker} annulé: prix indisponible")
                    continue
                current_price = ticker_data["price"]
                atr_pct       = ticker_data.get("atr_pct")
                stock_name    = ticker
            else:
                current_price = quote["price"]
                atr_pct = None
                stock_name = quote.get("name", ticker)
                # Récupérer ATR via yfinance
                td = analyze_ticker(ticker, period="2mo")
                atr_pct = td.get("atr_pct") if td else None

            # Calcul de la quantité
            quantity = risk_mgr.compute_quantity(
                ticker       = ticker,
                quantity_pct = act["quantity_pct"],
                portfolio_value = total_value,
                current_price   = current_price,
                atr_pct         = atr_pct,
            )

            if quantity <= 0:
                log.warning(f"BUY {ticker} annulé: quantité calculée = 0")
                continue

            # Validation risk
            ok, msg = risk_mgr.can_buy(ticker, quantity, current_price, portfolio, regime)
            if not ok:
                log.warning(f"BUY {ticker} refusé par risk manager: {msg}")
                continue

            # Exécution de l'achat
            try:
                result = client.buy(
                    ticker   = ticker,
                    price    = current_price,
                    quantity = quantity,
                    name     = stock_name,
                    market   = "CA" if ticker.endswith(".TO") else "US",
                )
                if result.get("error"):
                    log.warning(f"BUY {ticker} échoué: {result}")
                    continue

                executed_buys += 1
                confidence = act.get("confidence", "?")
                log.info(f"BUY {ticker} exécuté: {quantity:.2f} @ ${current_price:.2f} | confiance={confidence}/10 | {act.get('reasoning', '')}")

                # Création du stop loss automatique
                stop_pct   = act.get("stop_loss_pct", config.DEFAULT_STOP_LOSS_PCT)
                profit_pct = act.get("take_profit_pct", config.DEFAULT_STOP_LOSS_PCT * 2)
                stop_price   = risk_mgr.compute_stop_loss_price(current_price, stop_pct)
                profit_price = risk_mgr.compute_take_profit_price(current_price, profit_pct)

                try:
                    client.create_stop_loss(ticker, stop_price, quantity, stock_name)
                    client.create_take_profit(ticker, profit_price, quantity, stock_name)
                except Exception as e:
                    log.warning(f"Impossible de créer stop loss/take profit pour {ticker}: {e}")

                # Rafraîchir le portfolio après achat
                portfolio = client.get_portfolio()
                total_value = portfolio.get("portfolio_value", total_value)

            except Exception as e:
                log.error(f"Erreur BUY {ticker}: {e}")

    log.info(f"Cycle terminé: {executed_buys} achat(s), {executed_sells} vente(s)")


# ── Tâches planifiées ─────────────────────────────────────────────────────────

def pre_market_analysis():
    """Exécuté à 9h05 EST (avant ouverture). Analyse rapide du régime."""
    log.info("=== PRÉ-MARCHÉ ===")
    try:
        market = get_market_regime()
        regime = market.get("regime", "neutral")
        vix    = market.get("vix", "?")
        spy_ch = market.get("spy_change", "?")
        log.info(f"Régime pré-marché: {regime.upper()} | VIX={vix} | SPY={spy_ch:+}%")
        # Initialiser le suivi journalier
        portfolio = client.get_portfolio()
        risk_mgr.start_of_day(portfolio.get("portfolio_value", 0))
    except Exception as e:
        log.error(f"Erreur pré-marché: {e}")


def end_of_day_summary():
    """Exécuté à 16h05 EST (après clôture). Résumé journalier."""
    log.info("=== FIN DE JOURNÉE ===")
    try:
        portfolio = client.get_portfolio()
        total     = portfolio.get("portfolio_value", 0)
        cash      = portfolio.get("cash", 0)
        pnl       = portfolio.get("pnl_pct", 0)
        positions = portfolio.get("positions", [])

        log.info(f"Portfolio final: ${total:,.2f} | Cash: ${cash:,.0f} | P&L: {pnl:+.2f}%")
        log.info(f"Positions ouvertes ({len(positions)}):")
        for p in positions:
            log.info(f"  {p['ticker']:12} {p['quantity']:.2f} actions | P&L: {p.get('pnl_pct', 0):+.1f}%")

        trades = client.get_trades()
        today_trades = [
            t for t in trades
            if t.get("executed_at", "")[:10] == datetime.now(EST).strftime("%Y-%m-%d")
        ]
        log.info(f"Trades aujourd'hui: {len(today_trades)}")
    except Exception as e:
        log.error(f"Erreur résumé journalier: {e}")


def off_hours_analysis():
    """
    Analyse du marché hors-heures (toutes les 4h, 24/7).
    Régime + top candidats pour anticiper l'ouverture.
    """
    if is_market_open():
        return  # Le cycle principal gère déjà ça
    now = datetime.now(EST)
    mins = minutes_to_open()
    log.info(f"=== ANALYSE HORS-MARCHÉ ({now.strftime('%H:%M')} EST | ouverture dans {mins} min) ===")
    try:
        market = get_market_regime()
        regime = market.get("regime", "neutral")
        vix    = market.get("vix", "?")
        spy_ch = market.get("spy_change", "?")
        gold_ch = market.get("gold_change", "?")
        dxy    = market.get("dxy", "?")
        log.info(f"Régime: {regime.upper()} | VIX={vix} | SPY={spy_ch:+}% | Or={gold_ch:+}% | DXY={dxy}")
    except Exception as e:
        log.error(f"Erreur analyse régime hors-marché: {e}")
        regime = "neutral"
        return

    try:
        candidates = get_top_candidates(n=10, regime=regime)
        log.info(f"Top 10 candidats pré-calculés pour lundi:")
        for c in candidates[:10]:
            log.info(
                f"  {c['ticker']:12} score={c['score']:5.1f} | "
                f"RSI={c.get('rsi') or '?':>5} | "
                f"MACD_hist={c.get('macd_hist') or '?':>7} | "
                f"${c['price']:.2f}"
            )
    except Exception as e:
        log.error(f"Erreur analyse candidats hors-marché: {e}")


def hourly_job():
    """Job principal exécuté toutes les heures."""
    if is_market_open():
        run_trading_cycle()
    else:
        now = datetime.now(EST)
        mins = minutes_to_open()
        log.info(f"Marché fermé ({now.strftime('%H:%M')} EST) — prochaine ouverture dans {mins} min")


# ── Point d'entrée ────────────────────────────────────────────────────────────

def main():
    log.info("╔══════════════════════════════════════╗")
    log.info("║     TRADING BOT — DÉMARRAGE          ║")
    log.info("╚══════════════════════════════════════╝")
    log.info(f"Site:    {config.FINANCEACADEMY_URL}")
    log.info(f"LLM:     {config.LLM_BASE_URL} ({config.LLM_MODEL})")
    log.info(f"Config:  max_pos={config.MAX_POSITIONS} | max_pct={config.MAX_POSITION_PCT}% | stop={config.DEFAULT_STOP_LOSS_PCT}%")

    # Test de connexion au site
    log.info("Test de connexion à FinanceAcademy...")
    try:
        portfolio = client.get_portfolio()
        total = portfolio.get("portfolio_value", portfolio.get("cash", 0))
        log.info(f"Connexion OK | Portfolio: ${total:,.2f}")
    except Exception as e:
        log.error(f"Impossible de se connecter à FinanceAcademy: {e}")
        log.error("Vérifie FINANCEACADEMY_URL et les credentials dans .env")
        return

    # Test LM Studio
    log.info("Test de connexion à LM Studio...")
    if agent.is_available():
        log.info(f"LM Studio OK | Modèle: {config.LLM_MODEL}")
    else:
        log.warning(f"LM Studio inaccessible sur {config.LLM_BASE_URL}")
        log.warning("Le bot tournera mais mettra les cycles en pause jusqu'à ce que LM Studio réponde")

    # Scheduler
    scheduler = BlockingScheduler(timezone=EST)

    # Analyse pré-marché tous les jours de semaine à 9h05
    scheduler.add_job(
        pre_market_analysis,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=5, timezone=EST),
        id="pre_market",
        name="Analyse pré-marché",
    )

    # Résumé fin de journée
    scheduler.add_job(
        end_of_day_summary,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=5, timezone=EST),
        id="end_of_day",
        name="Résumé fin de journée",
    )

    # Cycle principal toutes les heures
    scheduler.add_job(
        hourly_job,
        IntervalTrigger(minutes=config.CYCLE_INTERVAL_MIN),
        id="main_cycle",
        name="Cycle de trading",
        next_run_time=datetime.now(EST),  # Exécute immédiatement au démarrage
    )

    # Analyse hors-marché toutes les 4h (régime + candidats, même le weekend)
    scheduler.add_job(
        off_hours_analysis,
        IntervalTrigger(hours=4),
        id="off_hours",
        name="Analyse hors-marché",
        next_run_time=datetime.now(EST) + timedelta(minutes=2),  # 2 min après démarrage
    )

    log.info(f"Scheduler démarré — cycle toutes les {config.CYCLE_INTERVAL_MIN} min")
    log.info("Ctrl+C pour arrêter")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Bot arrêté manuellement")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
