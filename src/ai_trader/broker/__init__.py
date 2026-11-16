"""券商层：统一接口 + 模拟撮合 + 星财富真实适配器。"""
from .base import (
    AccountInfo,
    AccountSummary,
    Broker,
    BrokerError,
    BrokerNotConfigured,
    Candle,
    OrderRejected,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
)
from .paper import PaperBroker, SyntheticQuoteSource, QuoteSource, market_of, currency_of
from .fosun_adapter import FosunBroker

__all__ = [
    "AccountInfo", "AccountSummary", "Broker", "BrokerError", "BrokerNotConfigured",
    "Candle", "OrderRejected", "OrderRequest", "OrderResult", "Position", "Quote",
    "PaperBroker", "SyntheticQuoteSource", "QuoteSource", "market_of", "currency_of",
    "FosunBroker",
]
