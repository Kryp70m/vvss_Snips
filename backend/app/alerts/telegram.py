import logging
import httpx
from app.core.config import Settings
from app.models.scanner import AlertEvent

logger = logging.getLogger(__name__)

class TelegramAlerter:
    """Production V2 Alerter handle integrated with ScannerService loop structures."""
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.client = httpx.AsyncClient(timeout=10.0)

    async def connect(self) -> None:
        """Initializes connection resource handles on application engine startup."""
        logger.info("Telegram notification pipeline successfully initialized.")

    async def close(self) -> None:
        """Gracefully terminates internal asynchronous connection fabrics."""
        await self.client.aclose()

    async def send_alert(self, alert: AlertEvent) -> None:
        """Transmits baseline momentum breakouts directly to the configured chat room."""
        if not self.bot_token or not self.chat_id:
            return

        message = (
            "🚨 *Momentum Ignition Alert* 🚨\n\n"
            f"• *Asset:* {alert.symbol} ({alert.exchange.upper()})\n"
            f"• *Direction:* {alert.direction.upper()}\n"
            f"• *Current Price:* ${alert.last_price:.4f}\n"
            f"• *Probability Score:* {alert.score:.1f}%\n"
            f"• *Label:* {alert.label}\n"
            f"• *Expected Move:* {alert.expected_move:.2f}%\n"
        )
        
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            response = await self.client.post(url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "Markdown"
            })
            if response.status_code != 200:
                logger.error(f"Telegram transmission boundaries failed: {response.text}")
        except Exception as e:
            logger.exception(f"Unexpected connectivity threshold interruption during alert delivery: {e}")


async def send_telegram_alert(symbol: str, side: str, price: float, raw_vol: float, smc_info: dict) -> None:
    """
    Standalone V2 global function utilized specifically to transmit high-conviction 
    institutional SMC/ICT setups with targeted Stop Loss and Take Profit boundaries.
    """
    from app.core.config import get_settings
    settings = get_settings()
    
    bot_token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    
    if not bot_token or not chat_id:
        return

    icon = "🟢 LONG" if side.upper() == "BUY" else "🔴 SHORT"
    
    message = (
        f"🔥 *SMC V2 HIGH CONVICTION SIGNAL* 🔥\n\n"
        f"• *Symbol:* {symbol}\n"
        f"• *Action:* {icon}\n"
        f"• *Market Price:* ${price:.4f}\n"
        f"• *Relative Vol Multiplier:* {raw_vol:.2f}x\n\n"
        f"🏆 *Institutional Framework Parameters:*\n"
        f"  - *Trend Bias:* {smc_info.get('bias', 'NEUTRAL')}\n"
        f"  - *Suggested Entry:* ${smc_info.get('entry', price):.4f}\n"
        f"  - *Invalidation SL:* ${smc_info.get('stop_loss', 0.0):.4f}\n"
        f"  - *Target Profit Matrix:* ${smc_info.get('take_profit', 0.0):.4f}\n\n"
        f"📝 *Reasoning Context:* \n_{smc_info.get('reasoning', '')}_"
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(url, json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown"
            })
            if response.status_code != 200:
                logger.error(f"Telegram High-Conviction payload rejected: {response.text}")
        except Exception as e:
            logger.error(f"Failed transmitting institutional matrix package: {e}")