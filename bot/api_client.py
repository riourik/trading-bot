"""
Client HTTP pour l'API FinanceAcademy.
Gère l'authentification JWT, le refresh automatique, et tous les appels trading.
"""
import time
import requests
from bot.logger import get_logger
import config

log = get_logger(__name__)


class FinanceAcademyClient:
    def __init__(self):
        self.base_url = config.FINANCEACADEMY_URL.rstrip("/") + "/api/android"
        self.token: str | None = None
        self.token_expires_at: float = 0
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _auth_headers(self) -> dict:
        self._ensure_token()
        return {"Authorization": f"Bearer {self.token}"}

    def _ensure_token(self):
        if self.token and time.time() < self.token_expires_at - 60:
            return
        self._login()

    def _call(self, method: str, path: str, **kwargs) -> requests.Response:
        """
        Appel API centralisé avec re-authentification automatique sur 401.
        Retry une fois si le serveur rejette le token (redémarrage, expiration serveur...).
        """
        url = f"{self.base_url}{path}"
        for attempt in range(2):
            r = getattr(self.session, method)(
                url,
                headers=self._auth_headers(),
                **kwargs,
            )
            if r.status_code == 401 and attempt == 0:
                log.warning("Token rejeté (401) — re-authentification forcée...")
                self.token = None
                self.token_expires_at = 0
                self._login()
                continue
            return r
        return r

    def _login(self):
        """Login ou register si le compte n'existe pas encore."""
        try:
            r = self.session.post(
                f"{self.base_url}/auth/login",
                json={"email": config.BOT_EMAIL, "password": config.BOT_PASSWORD},
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                self.token = data["access_token"]
                self.token_expires_at = time.time() + data.get("expires_in", 3600)
                log.info("Authentification réussie")
                return
            if r.status_code == 401:
                log.info("Compte inexistant, création du compte bot...")
                self._register()
        except Exception as e:
            log.error(f"Erreur de connexion à FinanceAcademy: {e}")
            raise

    def _register(self):
        r = self.session.post(
            f"{self.base_url}/auth/register",
            json={
                "username": config.BOT_USERNAME,
                "email": config.BOT_EMAIL,
                "password": config.BOT_PASSWORD,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        self.token = data["access_token"]
        self.token_expires_at = time.time() + 3600
        log.info(f"Compte bot créé: {config.BOT_USERNAME}")

    # ── Portfolio ─────────────────────────────────────────────────────────────

    def get_portfolio(self) -> dict:
        """Retourne {cash, portfolio_value, pnl, pnl_pct, positions[]}"""
        r = self._call("get", "/positions", timeout=20)
        r.raise_for_status()
        return r.json()

    def get_me(self) -> dict:
        r = self._call("get", "/users/me", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_trades(self) -> list:
        r = self._call("get", "/trades", timeout=15)
        r.raise_for_status()
        return r.json()

    # ── Trading ───────────────────────────────────────────────────────────────

    def buy(self, ticker: str, price: float, quantity: float, name: str = "", market: str = "US") -> dict:
        """Achète quantity actions de ticker au prix price."""
        payload = {
            "ticker": ticker.upper(),
            "price": round(price, 4),
            "quantity": round(quantity, 6),
            "name": name or ticker,
            "market": market,
        }
        r = self._call("post", "/positions", json=payload, timeout=20)
        if r.status_code == 400 and "insuffisants" in r.text:
            log.warning(f"Fonds insuffisants pour acheter {ticker}")
            return {"error": "insufficient_funds", "detail": r.json()}
        r.raise_for_status()
        result = r.json()
        log.info(f"ACHAT {ticker} x{quantity:.2f} @ ${price:.2f} = ${quantity*price:.2f}")
        return result

    def sell(self, ticker: str, quantity: float | None = None) -> dict:
        """Vend quantity actions (ou tout si quantity=None)."""
        payload = {}
        if quantity is not None:
            payload["quantity"] = round(quantity, 6)
        r = self._call("delete", f"/positions/{ticker.upper()}", json=payload, timeout=20)
        if r.status_code == 404:
            log.warning(f"Position {ticker} introuvable pour vente")
            return {"error": "not_found"}
        r.raise_for_status()
        result = r.json()
        log.info(f"VENTE {ticker} x{result.get('quantity', '?')} @ ${result.get('price', '?'):.2f}")
        return result

    # ── Ordres conditionnels ──────────────────────────────────────────────────

    def create_stop_loss(self, ticker: str, trigger_price: float, quantity: float, name: str = "") -> dict:
        """Crée un stop loss automatique."""
        payload = {
            "ticker": ticker.upper(),
            "name": name or ticker,
            "order_type": "stop_loss",
            "trigger_price": round(trigger_price, 4),
            "quantity": round(quantity, 6),
        }
        r = self._call("post", "/orders", json=payload, timeout=15)
        r.raise_for_status()
        log.info(f"Stop loss créé: {ticker} @ ${trigger_price:.2f}")
        return r.json()

    def create_take_profit(self, ticker: str, trigger_price: float, quantity: float, name: str = "") -> dict:
        """Crée un take profit automatique."""
        payload = {
            "ticker": ticker.upper(),
            "name": name or ticker,
            "order_type": "take_profit",
            "trigger_price": round(trigger_price, 4),
            "quantity": round(quantity, 6),
        }
        r = self._call("post", "/orders", json=payload, timeout=15)
        r.raise_for_status()
        log.info(f"Take profit créé: {ticker} @ ${trigger_price:.2f}")
        return r.json()

    def get_active_orders(self) -> list:
        r = self._call("get", "/orders", timeout=15)
        r.raise_for_status()
        return r.json().get("active", [])

    def cancel_order(self, order_id: int) -> dict:
        r = self._call("delete", f"/orders/{order_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    # ── Données marché via l'API ──────────────────────────────────────────────

    def get_quote(self, ticker: str) -> dict | None:
        """Prix temps réel pour n'importe quel ticker."""
        try:
            r = self._call("get", "/market/quote", params={"ticker": ticker.upper()}, timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"get_quote({ticker}) failed: {e}")
            return None

    def get_indices(self) -> list:
        """VIX, S&P 500, TSX, Gold, BTC, DXY..."""
        try:
            r = self._call("get", "/market/indices", timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"get_indices() failed: {e}")
            return []

    def get_stock_catalogue(self) -> list:
        """Tous les stocks US+CA avec prix."""
        try:
            r = self._call("get", "/stocks", timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"get_stock_catalogue() failed: {e}")
            return []
