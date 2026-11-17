"""真实行情接入层（Provider Adapter）。

对外只暴露与"接行情"有关的类型；交易决策仍由既有风控/门禁负责。
"""
from .adapters import (AlpacaProvider, DatabentoProvider, MassiveProvider,
                       NasdaqItchProvider, RecordedProvider)
from .authority import (AuthorityRouter, AuthorityVerdict, EventSignatureVerifier,
                        NoSignatureVerifier, ProviderAuthoritySpec,
                        SignatureStatus, surveyed_specs)
from .base import (MarketDataProvider, ProviderCapabilities, ProviderError,
                   ProviderNotConfigured, ProviderUnavailable)
from .feed import FeedUnavailable, ProviderFeed, SymbolState
from .fosun import (FOSUN_VENUE, FosunDataError, FosunKlineBar,
                    FosunOrderBookSnapshot, FosunProvider,
                    FosunQuoteSnapshot, parse_kline_row, parse_orderbook,
                    parse_quote, parse_tick)
from .models import (BLOCKING_FLAGS, DataQuality, EventAuthority, EventType,
                     HaltEvent, LULDState, MWCBState, MarketEvent,
                     OrderBookEvent, ProviderHealth, QualityFlag, QuoteEvent,
                     SessionEvent, SessionPhase, TradeEvent)
from .normalize import (normalize_exchange, normalize_symbol, parse_timestamp,
                        session_phase_of, to_market_time)

__all__ = [
    "AlpacaProvider", "DatabentoProvider", "MassiveProvider", "NasdaqItchProvider",
    "RecordedProvider", "AuthorityRouter", "AuthorityVerdict",
    "EventSignatureVerifier", "NoSignatureVerifier", "ProviderAuthoritySpec",
    "SignatureStatus", "surveyed_specs", "MarketDataProvider",
    "ProviderCapabilities", "ProviderError", "ProviderNotConfigured",
    "ProviderUnavailable", "FeedUnavailable", "ProviderFeed", "SymbolState",
    "BLOCKING_FLAGS", "DataQuality", "EventAuthority", "EventType", "HaltEvent",
    "LULDState", "MWCBState", "MarketEvent", "OrderBookEvent", "ProviderHealth",
    "QualityFlag", "QuoteEvent", "SessionEvent", "SessionPhase", "TradeEvent",
    "FOSUN_VENUE", "FosunDataError", "FosunKlineBar",
    "FosunOrderBookSnapshot", "FosunProvider", "FosunQuoteSnapshot",
    "parse_kline_row", "parse_orderbook", "parse_quote", "parse_tick",
    "normalize_exchange", "normalize_symbol", "parse_timestamp",
    "session_phase_of", "to_market_time",
]
