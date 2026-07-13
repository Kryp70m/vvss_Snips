import logging

import aiohttp

from app.models.scanner import AlertEvent

logger = logging.getLogger(__name__)


class TelegramAlerter:
    def __init__(self, token: str | None, chat_id: str | None) -> None:
        self.token = token
        self.chat_id = chat_id
        self.session: aiohttp.ClientSession | None = None

    async def connect(self) -> None:
        if self.token and self.chat_id:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def configure(self, token: str | None, chat_id: str | None) -> None:
        self.token = token.strip() if token else None
        self.chat_id = chat_id.strip() if chat_id else None
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))

    def status(self) -> dict:
        return {
            "enabled": bool(self.token and self.chat_id),
            "chat_id": self.chat_id or "",
            "token_set": bool(self.token),
        }

    async def send_text(self, text: str) -> None:
        if not self.session or not self.token or not self.chat_id:
            raise RuntimeError("Telegram is not configured")
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with self.session.post(url, json={"chat_id": self.chat_id, "text": text}) as response:
            response.raise_for_status()

    async def send(self, alert: AlertEvent) -> None:
        if not self.session or not self.token or not self.chat_id:
            return
        snap = alert.snapshot
        side = "BUY" if alert.direction == "long" else "SELL" if alert.direction == "short" else "NEUTRAL"
        text = (
            f"{snap.symbol}\n\n"
            f"Direction: {side}\n"
            f"Relative Volume: {snap.relative_volume:.2f}x\n"
            f"Aggressive Buy Flow: {snap.aggressive_buy_flow}\n"
            f"Aggressive Sell Flow: {snap.aggressive_sell_flow}\n"
            f"Compression: {'Confirmed' if snap.compression else 'No'}\n"
            f"NATR 5m/14: {snap.natr_5m_14:.2f}%\n"
            f"Liquidity Sensitivity: {snap.liquidity_label}\n"
            f"Expansion Efficiency: {snap.expansion_efficiency:.1f}\n\n"
            f"Impact Score: {snap.impact_score:.1f}/100\n"
            f"Manipulation Probability: {snap.manipulation_probability:.1f}/100 ({snap.manipulation_phase})\n"
            f"Distribution Strength: {snap.distribution_strength:.1f}/100 ({snap.distribution_phase})\n"
            f"Retracement Quality: {snap.retracement_quality:.1f}/100 ({snap.retracement_phase})\n"
            f"Continuation Probability: {snap.continuation_probability:.1f}/100\n"
            f"Expected Move %: {snap.expected_move_pct:.2f}%\n\n"
            f"Impulse: {snap.impulse_confirmation} ({snap.impulse_move_pct:.2f}% / need {snap.impulse_required_pct:.2f}%)\n"
            f"Entry: {alert.entry_price:.10g}\n"
            f"Confirm Price: {snap.entry_confirmation_price:.10g}\n"
            f"TP1: {snap.target_1_price:.10g}\n"
            f"TP2: {snap.target_2_price:.10g}\n"
            f"TP3: {snap.target_3_price:.10g}\n"
            f"Stop Loss: {alert.stop_loss_price:.10g}\n"
            f"Expected Move: {alert.expected_move}\n"
            f"Momentum Ignition Probability: {alert.label} ({alert.score:.1f})"
        )
        try:
            await self.send_text(text)
        except Exception:
            logger.exception("Failed to send Telegram alert for %s", alert.symbol)
