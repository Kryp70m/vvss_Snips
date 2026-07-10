import numpy as np
from typing import List, Dict, Optional, Tuple
from pydantic import BaseModel

class Candle(BaseModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

class SMCSetup(BaseModel):
    bias: str
    entry: float
    stop_loss: float
    take_profit: float
    reasoning: str
    high_conviction: bool

class SMCAnalyzer:
    def __init__(self, lookback: int = 50):
        self.lookback = lookback

    def analyze_structure(self, htf_candles: List[Candle], ltf_candles: List[Candle], correlated_candles: Optional[List[Candle]] = None) -> Optional[SMCSetup]:
        if len(htf_candles) < 5 or len(ltf_candles) < 5:
            return None
            
        # 1. Calculate HTF Bias
        htf_bias = self._calculate_bias(htf_candles)
        
        # 2. Extract LTF Conditions
        last_c = ltf_candles[-1]
        prev_c1 = ltf_candles[-2]
        prev_c2 = ltf_candles[-3]
        
        # 3. Check for FVG (Fair Value Gap)
        bullish_fvg = ltf_candles[-1].low > ltf_candles[-3].high
        bearish_fvg = ltf_candles[-1].high < ltf_candles[-3].low
        
        # 4. SMT Divergence Filter (Correlated assets)
        smt_trap = False
        if correlated_candles and len(correlated_candles) >= 2:
            # Check if asset made a lower low but correlated asset made a higher low (SMT)
            if ltf_candles[-1].low < ltf_candles[-2].low and correlated_candles[-1].low > correlated_candles[-2].low:
                smt_trap = True

        # 5. Position Formulation based on HTF Alignments
        if htf_bias == "BULLISH" and bullish_fvg and not smt_trap:
            entry = ltf_candles[-2].high
            sl = min([c.low for c in ltf_candles[-5:]])
            tp = entry + (entry - sl) * 2.5
            return SMCSetup(
                bias="BULLISH", entry=entry, stop_loss=sl, take_profit=tp,
                reasoning="HTF Bullish alignment confirmed with LTF FVG entry space. SMT Trap checked.",
                high_conviction=True
            )
            
        elif htf_bias == "BEARISH" and bearish_fvg and not smt_trap:
            entry = ltf_candles[-2].low
            sl = max([c.high for c in ltf_candles[-5:]])
            tp = entry - (sl - entry) * 2.5
            return SMCSetup(
                bias="BEARISH", entry=entry, stop_loss=sl, take_profit=tp,
                reasoning="HTF Bearish alignment confirmed with LTF FVG short entry space. No SMT Trap observed.",
                high_conviction=True
            )
            
        return None

    def _calculate_bias(self, candles: List[Candle]) -> str:
        closes = [c.close for c in candles[-5:]]
        if closes[-1] > closes[0]:
            return "BULLISH"
        return "BEARISH"