"""
Agent LLM connecté à LM Studio (API compatible OpenAI).
Construit le contexte de marché, envoie au LLM, parse et valide la réponse.
"""
import json
import re
import requests
from bot.logger import get_logger
import config

log = get_logger(__name__)

SYSTEM_PROMPT = """Tu es un trader quantitatif professionnel gérant un portfolio simulé en USD.
Ton objectif est de maximiser les rendements tout en gérant le risque selon les conditions du marché.

CALIBRAGE PAR CONVICTION (confidence 1-10) — pense comme un humain qui gère $10,000:
- Conviction FORTE (8-10)   : 10-15% du portfolio → seulement si TOUS les signaux sont alignés
- Conviction MODÉRÉE (6-7)  : 5-8% du portfolio → bonne opportunité avec quelques réserves
- Conviction FAIBLE (4-5)   : 2-4% du portfolio → position test ou spéculative
- Conviction < 4             : ne pas acheter, HOLD

RÈGLES PAR RÉGIME:
- Marché BULL : déployer jusqu'à 85% du capital, 4-8 positions concentrées
- Marché BEAR : mode défensif, max 50% déployé, conviction 6+ seulement, positions 2-5%
- Marché NEUTRAL : 65-75% déployé, conviction 5+ seulement, 3-6% par position
- Ne jamais détenir plus de 12 positions simultanément
- Scale-in autorisé : tu peux racheter un ticker déjà en portefeuille si la position totale reste sous MAX_POSITION_PCT
- Si tu as du cash disponible et des positions existantes performantes, ajoute-y plutôt que laisser le cash dormir
- Diversifier les secteurs (max 3 positions par secteur)
- Ne jamais acheter un stock dont le RSI > 75 (surachat extrême)
- Vendre si une position perd > 8% (stop mental)

PROCESSUS DE DÉCISION (à suivre avant chaque trade):
1. ANALYSE MACRO : valide le régime marché avec VIX, SPY vs SMA200, Gold, DXY
2. DÉBAT INTERNE : pour chaque candidat sérieux, pèse mentalement le cas HAUSSIER vs BAISSIER
   - Cas HAUSSIER : momentum technique, secteur fort, catalyseurs positifs dans les news
   - Cas BAISSIER : risques, news négatives, RSI élevé, tendance faible
3. FILTRE NEWS : si une news récente annonce un risque majeur (procès, faillite, scandale), évite le titre même si le score technique est bon
4. DÉCISION : n'achète que si le cas haussier est clairement dominant. En cas de doute → HOLD

FORMAT DE RÉPONSE: Tu dois répondre UNIQUEMENT avec un JSON valide, sans texte avant ou après.
Structure exacte requise:
{
  "regime_assessment": "bull|bear|neutral",
  "risk_profile": "aggressive|moderate|defensive",
  "market_commentary": "Analyse du marché en 1-2 phrases.",
  "actions": [
    {
      "action": "buy|sell|hold",
      "ticker": "AAPL",
      "quantity_pct": 3.5,
      "reasoning": "Cas haussier: ... | Cas baissier: ... | Décision: ...",
      "stop_loss_pct": 6.0,
      "take_profit_pct": 12.0,
      "confidence": 7
    }
  ]
}
- quantity_pct = % du portfolio total selon ta conviction: confidence 9 → 12-15%, confidence 7 → 6-8%, confidence 5 → 3-4%
- stop_loss_pct = % de baisse sous le prix d'achat pour déclencher le stop
- take_profit_pct = % de hausse pour prendre les profits (vise au moins 2x le stop loss)
- confidence = conviction 1-10 : doit être cohérent avec quantity_pct
- actions peut être vide [] si aucun trade n'est justifié
"""


def _build_user_prompt(portfolio: dict, market: dict, candidates: list[dict], news: dict = None) -> str:
    """Construit le message utilisateur avec tout le contexte."""

    # Résumé du marché
    regime = market.get("regime", "neutral").upper()
    vix    = market.get("vix", "?")
    vix_r  = market.get("vix_regime", "?")
    spy    = market.get("spy_price", "?")
    spy_ch = market.get("spy_change", "?")
    spy_200 = "AU-DESSUS" if market.get("spy_above_200") else "EN-DESSOUS"
    tsx    = market.get("tsx_price", "?")
    tsx_ch = market.get("tsx_change", "?")
    gold   = market.get("gold_price", "?")
    gold_ch= market.get("gold_change", "?")
    dxy    = market.get("dxy", "?")
    dxy_ch = market.get("dxy_change", "?")
    spy_rsi = market.get("spy_rsi", "?")

    market_block = f"""=== ANALYSE DU MARCHÉ ===
Régime détecté: {regime}
S&P 500 (SPY): ${spy} ({spy_ch:+}%) | vs SMA200: {spy_200} | RSI={spy_rsi}
TSX Composite: {tsx} ({tsx_ch:+}%)
VIX: {vix} → Peur marché: {vix_r.upper()}
Or (Gold): ${gold} ({gold_ch:+}%) — signal risk-off: {"OUI" if market.get("gold_rising") else "NON"}
Dollar Index (DXY): {dxy} ({dxy_ch:+}%)
"""

    # État du portfolio
    cash  = portfolio.get("cash", 0)
    total = portfolio.get("portfolio_value", cash)
    pnl   = portfolio.get("pnl", 0)
    pnl_p = portfolio.get("pnl_pct", 0)
    positions = portfolio.get("positions", [])
    cash_pct  = round(cash / total * 100, 1) if total > 0 else 100

    pos_lines = []
    for p in positions:
        pos_lines.append(
            f"  {p['ticker']}: {p['quantity']:.2f} actions @ moy ${p['avg_price']:.2f} "
            f"| actuel ~${p.get('current_value', 0) / p['quantity']:.2f} "
            f"| P&L: {p.get('pnl_pct', 0):+.1f}%"
        )
    pos_block = "\n".join(pos_lines) if pos_lines else "  (aucune position ouverte)"

    portfolio_block = f"""
=== ÉTAT DU PORTFOLIO ===
Valeur totale: ${total:,.2f}
Cash disponible: ${cash:,.2f} ({cash_pct}%)
Capital déployé: ${total - cash:,.2f} ({100 - cash_pct:.1f}%)
P&L global: ${pnl:+,.2f} ({pnl_p:+.2f}%)
Positions ouvertes ({len(positions)}):
{pos_block}
"""

    # Top candidats
    def fmt_candidate(s: dict) -> str:
        rsi  = s.get("rsi", "?")
        macd = s.get("macd_hist")
        macd_str = f"{macd:+.4f}" if macd is not None else "?"
        bb   = s.get("bb_pct")
        bb_str = f"{bb:.2f}" if bb is not None else "?"
        above = []
        if s.get("above_sma20"):  above.append("SMA20")
        if s.get("above_sma50"):  above.append("SMA50")
        if s.get("above_sma200"): above.append("SMA200")
        trend = ">".join(above) if above else "sous toutes SMAs"
        return (
            f"  {s['ticker']:12} ${s['price']:>8.2f} ({s['change']:+.1f}%) "
            f"| Score={s.get('score',0):4.0f} | RSI={rsi} "
            f"| MACD_hist={macd_str} | BB%={bb_str} | Tendance: {trend}"
        )

    cand_block = "\n".join(fmt_candidate(c) for c in candidates)

    # Bloc news (seulement pour les candidats qui ont des news)
    news_lines = []
    if news:
        for c in candidates:
            ticker = c["ticker"]
            headlines = news.get(ticker)
            if headlines:
                news_lines.append(f"  {ticker}:")
                for h in headlines:
                    news_lines.append(f"    • {h}")
    news_block = ""
    if news_lines:
        news_block = "\n=== NEWS RÉCENTES (top candidats) ===\n" + "\n".join(news_lines) + "\n"

    task = f"""
=== CANDIDATS D'ACHAT (triés par score technique) ===
{cand_block}
{news_block}
=== TÂCHE ===
Applique ton processus de décision (débat haussier/baissier + filtre news).
Tiens compte:
1. Du régime ({regime}) pour calibrer l'agressivité
2. Des positions existantes (évite les doublons, surveille les pertes > 8%)
3. De la liquidité disponible (${cash:,.0f} cash, {cash_pct}%)
4. De la diversification sectorielle
5. Des news récentes — une mauvaise news annule un bon score technique

Réponds UNIQUEMENT avec le JSON demandé, sans texte supplémentaire.
"""

    return market_block + portfolio_block + task


class LLMAgent:
    def __init__(self):
        self.base_url = config.LLM_BASE_URL.rstrip("/")
        self.model    = config.LLM_MODEL
        self.headers  = {
            "Authorization": f"Bearer {config.LLM_API_KEY}",
            "Content-Type": "application/json",
        }

    def is_available(self) -> bool:
        """Vérifie que LM Studio répond."""
        try:
            r = requests.get(
                f"{self.base_url}/models",
                headers=self.headers,
                timeout=5,
            )
            return r.status_code == 200
        except Exception:
            return False

    def decide(self, portfolio: dict, market: dict, candidates: list[dict], news: dict = None) -> dict | None:
        """
        Envoie le contexte au LLM et retourne les décisions parsées.
        Retourne None en cas d'échec complet.
        """
        user_prompt = _build_user_prompt(portfolio, market, candidates, news=news)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens":  4096,   # Augmenté pour éviter les JSON tronqués
            "stream": False,
        }

        log.info(f"Envoi au LLM ({self.model})...")
        for attempt in range(1, 3):  # Max 2 tentatives
            try:
                r = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json=payload,
                    timeout=config.LLM_TIMEOUT,
                )
                r.raise_for_status()
                raw_content = r.json()["choices"][0]["message"]["content"].strip()
                log.debug(f"Réponse LLM brute:\n{raw_content[:500]}")
                result = self._parse_response(raw_content)
                if result:
                    return result
                if attempt < 2:
                    log.warning(f"JSON invalide (tentative {attempt}/2) — retry...")
                    # Retry avec prompt simplifié pour forcer un JSON plus court
                    payload["messages"][-1]["content"] = (
                        user_prompt +
                        "\n\nIMPORTANT: Limite tes actions à 3 maximum pour rester concis. "
                        "Réponds UNIQUEMENT avec le JSON, sans texte."
                    )
            except requests.Timeout:
                log.error(f"LLM timeout après {config.LLM_TIMEOUT}s (tentative {attempt}/2)")
                if attempt >= 2:
                    return None
            except Exception as e:
                log.error(f"Erreur LLM (tentative {attempt}/2): {e}")
                if attempt >= 2:
                    return None
        return None

    def _parse_response(self, raw: str) -> dict | None:
        """Parse et valide le JSON retourné par le LLM."""
        # Extraire le JSON (le LLM peut ajouter du texte avant/après)
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            log.error("Aucun JSON trouvé dans la réponse LLM")
            return None

        try:
            data = json.loads(json_match.group(0))
        except json.JSONDecodeError as e:
            log.error(f"JSON invalide dans la réponse LLM: {e}")
            return None

        # Validation de la structure
        if "actions" not in data:
            log.error("Champ 'actions' manquant dans la réponse LLM")
            return None

        validated_actions = []
        for a in data.get("actions", []):
            if not isinstance(a, dict):
                continue
            action = a.get("action", "").lower()
            ticker = str(a.get("ticker", "")).upper().strip()
            qty_pct = float(a.get("quantity_pct", 0))

            if action not in ("buy", "sell", "hold"):
                log.warning(f"Action invalide ignorée: {a}")
                continue
            if not ticker:
                continue
            if action == "buy" and (qty_pct <= 0 or qty_pct > 10):
                log.warning(f"quantity_pct hors limites ({qty_pct}) pour {ticker}, corrigé à 3%")
                qty_pct = 3.0

            confidence = max(1, min(10, int(a.get("confidence", 5))))
            validated_actions.append({
                "action":          action,
                "ticker":          ticker,
                "quantity_pct":    min(qty_pct, config.MAX_POSITION_PCT),
                "reasoning":       str(a.get("reasoning", ""))[:200],
                "stop_loss_pct":   float(a.get("stop_loss_pct", config.DEFAULT_STOP_LOSS_PCT)),
                "take_profit_pct": float(a.get("take_profit_pct", config.DEFAULT_STOP_LOSS_PCT * 2)),
                "confidence":      confidence,
            })

        data["actions"] = validated_actions
        log.info(
            f"LLM → régime={data.get('regime_assessment')} | "
            f"profil={data.get('risk_profile')} | "
            f"{len(validated_actions)} action(s)"
        )
        if data.get("market_commentary"):
            log.info(f"LLM commentaire: {data['market_commentary']}")

        return data
