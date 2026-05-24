"""
Gestionnaire de risque : sizing des positions, validation des trades,
stop loss, limites journalières, diversification.
"""
from bot.logger import get_logger
from bot.market_analyzer import analyze_ticker
import config

log = get_logger(__name__)


class RiskManager:
    def __init__(self):
        self._daily_start_value: float | None = None
        self._trading_halted: bool = False

    # ── État journalier ───────────────────────────────────────────────────────

    def start_of_day(self, portfolio_value: float):
        """Appeler à l'ouverture du marché pour initialiser le suivi journalier."""
        self._daily_start_value = portfolio_value
        self._trading_halted = False
        log.info(f"Début de journée: portfolio ${portfolio_value:,.2f}")

    def check_daily_loss_limit(self, current_value: float) -> bool:
        """
        Vérifie si la perte journalière dépasse la limite.
        Retourne True si le trading doit continuer, False si halted.
        """
        if self._trading_halted:
            return False
        if self._daily_start_value is None:
            return True

        daily_loss_pct = (current_value - self._daily_start_value) / self._daily_start_value * 100
        if daily_loss_pct <= -config.MAX_DAILY_LOSS_PCT:
            self._trading_halted = True
            log.warning(
                f"TRADING HALTED: perte journalière {daily_loss_pct:.2f}% "
                f"dépasse la limite de -{config.MAX_DAILY_LOSS_PCT}%"
            )
            return False
        return True

    @property
    def is_halted(self) -> bool:
        return self._trading_halted

    # ── Sizing des positions ──────────────────────────────────────────────────

    def compute_quantity(
        self,
        ticker: str,
        quantity_pct: float,
        portfolio_value: float,
        current_price: float,
        atr_pct: float | None = None,
    ) -> float:
        """
        Calcule le nombre d'actions à acheter.
        quantity_pct = % du portfolio à allouer (fourni par le LLM).
        Ajuste selon la volatilité (ATR) si disponible.
        """
        # Capital cible pour cette position
        target_amount = portfolio_value * (quantity_pct / 100)

        # Plafond absolu par position
        max_amount = portfolio_value * (config.MAX_POSITION_PCT / 100)
        target_amount = min(target_amount, max_amount)

        # Réduction si volatilité élevée (ATR > 3% du prix)
        if atr_pct and atr_pct > 3.0:
            reduction = min(atr_pct / 3.0, 2.0)  # Max division par 2
            target_amount /= reduction
            log.debug(f"{ticker}: position réduite x{reduction:.1f} (ATR={atr_pct:.1f}%)")

        if current_price <= 0:
            return 0

        quantity = target_amount / current_price
        # Arrondir à 2 décimales (actions fractionnées supportées)
        return round(quantity, 2)

    def compute_stop_loss_price(self, buy_price: float, stop_loss_pct: float) -> float:
        """Prix de déclenchement du stop loss."""
        return round(buy_price * (1 - stop_loss_pct / 100), 4)

    def compute_take_profit_price(self, buy_price: float, take_profit_pct: float) -> float:
        """Prix de déclenchement du take profit."""
        return round(buy_price * (1 + take_profit_pct / 100), 4)

    # ── Validation des trades ────────────────────────────────────────────────

    def can_buy(
        self,
        ticker: str,
        quantity: float,
        price: float,
        portfolio: dict,
        regime: str = "neutral",
    ) -> tuple[bool, str]:
        """
        Valide un achat avant exécution.
        Retourne (can_trade: bool, reason: str)
        """
        if self._trading_halted:
            return False, "Trading halté (perte journalière max atteinte)"

        cash     = portfolio.get("cash", 0)
        total    = portfolio.get("portfolio_value", cash)
        positions = portfolio.get("positions", [])

        # Vérification du nombre max de positions
        if len(positions) >= config.MAX_POSITIONS:
            return False, f"Max positions atteint ({config.MAX_POSITIONS})"

        # Position déjà ouverte sur ce ticker ?
        existing = next((p for p in positions if p["ticker"] == ticker), None)
        if existing:
            return False, f"Position déjà ouverte sur {ticker}"

        cost = quantity * price
        if cost > cash * 0.98:  # 2% de marge
            return False, f"Fonds insuffisants (besoin ${cost:.0f}, dispo ${cash:.0f})"

        # Cash minimum à conserver selon le régime
        min_cash_pct = {
            "bull":    config.MIN_CASH_PCT_BULL,
            "bear":    config.MIN_CASH_PCT_BEAR,
            "neutral": config.MIN_CASH_PCT_NEUTRAL,
        }.get(regime, config.MIN_CASH_PCT_NEUTRAL)

        min_cash_amount = total * (min_cash_pct / 100)
        if (cash - cost) < min_cash_amount:
            return False, (
                f"Cash résiduel trop bas: ${cash - cost:.0f} "
                f"< minimum ${min_cash_amount:.0f} ({min_cash_pct}% en mode {regime})"
            )

        # Capital max déployé selon régime
        max_deployed_pct = {"bull": 85, "bear": 55, "neutral": 75}.get(regime, 75)
        current_deployed_pct = (total - cash) / total * 100 if total > 0 else 0
        new_deployed_pct = (total - cash + cost) / total * 100
        if new_deployed_pct > max_deployed_pct:
            return False, (
                f"Capital déployé ({new_deployed_pct:.1f}%) "
                f"> max autorisé ({max_deployed_pct}%) en mode {regime}"
            )

        return True, "OK"

    def can_sell(self, ticker: str, quantity: float, portfolio: dict) -> tuple[bool, str]:
        """Valide une vente."""
        positions = portfolio.get("positions", [])
        pos = next((p for p in positions if p["ticker"] == ticker), None)
        if not pos:
            return False, f"Aucune position ouverte sur {ticker}"
        if quantity > pos["quantity"]:
            return False, f"Quantité ({quantity}) > position ({pos['quantity']})"
        return True, "OK"

    # ── Positions à surveiller ────────────────────────────────────────────────

    def check_positions_for_exit(self, portfolio: dict) -> list[dict]:
        """
        Vérifie chaque position ouverte et retourne celles qui doivent être fermées
        (stop loss déclenché, perte > seuil, ou signal de sortie technique).
        """
        positions = portfolio.get("positions", [])
        exits = []

        for pos in positions:
            ticker     = pos["ticker"]
            avg_price  = pos.get("avg_price", 0)
            pnl_pct    = pos.get("pnl_pct", 0)

            # Stop loss urgence : perte > DEFAULT_STOP_LOSS_PCT + 2% marge
            emergency_threshold = -(config.DEFAULT_STOP_LOSS_PCT + 2)
            if pnl_pct < emergency_threshold:
                exits.append({
                    "ticker":   ticker,
                    "quantity": pos["quantity"],
                    "reason":   f"Stop d'urgence: {pnl_pct:.1f}% (seuil {emergency_threshold}%)",
                })
                continue

            # Vérification technique supplémentaire
            data = analyze_ticker(ticker, period="1mo")
            if data:
                rsi = data.get("rsi")
                # RSI très overbought + perte en cours = sortie
                if rsi and rsi > 80 and pnl_pct > 5:
                    exits.append({
                        "ticker":   ticker,
                        "quantity": pos["quantity"],
                        "reason":   f"RSI={rsi} surachat + profit {pnl_pct:.1f}% — prise de profit",
                    })

        return exits

    def get_portfolio_summary(self, portfolio: dict, regime: str) -> str:
        """Résumé texte du portefeuille pour le logging."""
        cash  = portfolio.get("cash", 0)
        total = portfolio.get("portfolio_value", cash)
        pnl   = portfolio.get("pnl_pct", 0)
        n_pos = len(portfolio.get("positions", []))
        return (
            f"Portfolio: ${total:,.0f} | Cash: ${cash:,.0f} ({cash/total*100:.0f}%) "
            f"| Positions: {n_pos}/{config.MAX_POSITIONS} "
            f"| P&L: {pnl:+.2f}% | Régime: {regime.upper()}"
        )
