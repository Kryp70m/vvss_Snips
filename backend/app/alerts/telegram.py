import httpx
import logging
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

async def send_telegram_alert(symbol: str, side: str, price: float, raw_vol: float, smc_info: dict):
    """Dispatches high-conviction institutional signals directly to trading channels."""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return

    emoji = "🟢 INSTITUTIONAL LONG" if side == "BUY" else "🔴 INSTITUTIONAL SHORT"
    
    message = (
        f"{emoji} # {symbol}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Exchange: BINANCE Ticker Spike\n"
        f"• Execution Price: {price:.5f}\n"
        f"• Core Catalyst Vol: {raw_vol:,.2f}\n"
        f"• Structural Alignment: {smc_info.get('bias', 'NEUTRAL')}\n\n"
        f"🎯 SUGGESTED V2 ENTRIES:\n"
        f"• Entry Trigger: {smc_info.get('entry', 0.0):.5f}\n"
        f"• Invalidated Stop Loss: {smc_info.get('stop_loss', 0.0):.5f}\n"
        f"• Risk Profile Target (1:2.5): {smc_info.get('take_profit', 0.0):.5f}\n\n"
        f"📖 STRATEGY REASONING:\n"
        f"_{smc_info.get('reasoning', 'No structural reasons defined.')}_"
    )

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                logger.error(f"Telegram error boundary hit: {response.text}")
    except Exception as e:
        logger.error(f"Failed to transmit institutional signal metrics: {str(e)}")