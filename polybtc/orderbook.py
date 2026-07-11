from __future__ import annotations

from pydantic import BaseModel

from .models import BookLevel, OrderBookSnapshot


class ExecutionResult(BaseModel):
    complete: bool
    avg_price: float
    quantity: float
    quote: float
    slippage: float
    best_price: float | None
    available_depth_quote: float
    levels_used: int


def normalize_book(book: OrderBookSnapshot) -> OrderBookSnapshot:
    book.bids = sorted([lvl for lvl in book.bids if lvl.price > 0 and lvl.size > 0], key=lambda x: x.price, reverse=True)
    book.asks = sorted([lvl for lvl in book.asks if lvl.price > 0 and lvl.size > 0], key=lambda x: x.price)
    return book


def simulate_buy(book: OrderBookSnapshot, quote_usd: float) -> ExecutionResult:
    book = normalize_book(book)
    best = book.best_ask
    if best is None or quote_usd <= 0:
        return ExecutionResult(
            complete=False,
            avg_price=0.0,
            quantity=0.0,
            quote=0.0,
            slippage=0.0,
            best_price=best,
            available_depth_quote=0.0,
            levels_used=0,
        )

    remaining_quote = quote_usd
    quantity = 0.0
    quote = 0.0
    levels_used = 0
    available_depth_quote = sum(level.price * level.size for level in book.asks)

    for level in book.asks:
        if remaining_quote <= 1e-12:
            break
        level_quote = level.price * level.size
        take_quote = min(remaining_quote, level_quote)
        take_qty = take_quote / level.price
        quantity += take_qty
        quote += take_quote
        remaining_quote -= take_quote
        levels_used += 1

    avg_price = quote / quantity if quantity else 0.0
    return ExecutionResult(
        complete=remaining_quote <= 1e-9,
        avg_price=avg_price,
        quantity=quantity,
        quote=quote,
        slippage=avg_price - best if quantity else 0.0,
        best_price=best,
        available_depth_quote=available_depth_quote,
        levels_used=levels_used,
    )


def simulate_sell(book: OrderBookSnapshot, quantity: float) -> ExecutionResult:
    book = normalize_book(book)
    best = book.best_bid
    if best is None or quantity <= 0:
        return ExecutionResult(
            complete=False,
            avg_price=0.0,
            quantity=0.0,
            quote=0.0,
            slippage=0.0,
            best_price=best,
            available_depth_quote=0.0,
            levels_used=0,
        )

    remaining_qty = quantity
    filled_qty = 0.0
    quote = 0.0
    levels_used = 0
    available_depth_quote = sum(level.price * level.size for level in book.bids)

    for level in book.bids:
        if remaining_qty <= 1e-12:
            break
        take_qty = min(remaining_qty, level.size)
        filled_qty += take_qty
        quote += take_qty * level.price
        remaining_qty -= take_qty
        levels_used += 1

    avg_price = quote / filled_qty if filled_qty else 0.0
    return ExecutionResult(
        complete=remaining_qty <= 1e-9,
        avg_price=avg_price,
        quantity=filled_qty,
        quote=quote,
        slippage=best - avg_price if filled_qty else 0.0,
        best_price=best,
        available_depth_quote=available_depth_quote,
        levels_used=levels_used,
    )
