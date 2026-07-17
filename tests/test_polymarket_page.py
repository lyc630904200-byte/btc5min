import asyncio
import json
from datetime import datetime, timezone

from polybtc.clients import PolymarketClient, parse_polymarket_outcome_prices, parse_polymarket_past_results
from polybtc.config import SourceConfig


def test_parse_polymarket_past_results_from_hydration() -> None:
    html = '''
    {"results":[{"startTime":"2026-07-11T02:00:00.000Z","endTime":"2026-07-11T02:05:00.000Z","openPrice":63972.6322,"closePrice":64000.114765,"outcome":"up","percentChange":0.04},{"startTime":"2026-07-11T02:05:00.000Z","endTime":"2026-07-11T02:10:00.000Z","openPrice":64000.114765,"closePrice":64008.34514717294,"outcome":"up"}]}
    '''

    results = parse_polymarket_past_results(html)

    assert len(results) == 2
    assert results[0].start_time == datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    assert results[0].close_price == 64000.114765
    assert results[1].open_price == 64000.114765


def test_parse_polymarket_past_results_from_escaped_hydration() -> None:
    html = r'''
    {\"results\":[{\"startTime\":\"2026-07-11T02:10:00.000Z\",\"endTime\":\"2026-07-11T02:15:00.000Z\",\"openPrice\":64008.34514717294,\"closePrice\":63965.44320558364,\"outcome\":\"down\"}]}
    '''

    results = parse_polymarket_past_results(html)

    assert len(results) == 1
    assert results[0].start_time == datetime(2026, 7, 11, 2, 10, tzinfo=timezone.utc)
    assert results[0].close_price == 63965.44320558364


def test_parse_polymarket_outcome_price_for_current_slug() -> None:
    html = r'''
    {"state":{"data":{"openPrice":64081.30418196183,"closePrice":64123.73825},"status":"success"},"queryKey":["crypto-prices","price","BTC","2026-07-11T03:05:00Z","fiveminute","2026-07-11T03:10:00Z"]}
    {"state":{"data":{"openPrice":64123.73825,"closePrice":null},"status":"success"},"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:15:00Z"]}
    '''

    prices = parse_polymarket_outcome_prices(html)

    assert len(prices) == 2
    assert prices[0].slug == "btc-updown-5m-1783739100"
    assert prices[0].open_price == 64081.30418196183
    assert prices[0].close_price == 64123.73825
    assert prices[1].slug == "btc-updown-5m-1783739400"
    assert prices[1].open_price == 64123.73825
    assert prices[1].close_price is None
    assert prices[1].start_time == datetime(2026, 7, 11, 3, 10, tzinfo=timezone.utc)
    assert prices[1].end_time == datetime(2026, 7, 11, 3, 15, tzinfo=timezone.utc)


def test_outcome_parser_does_not_cross_react_query_objects() -> None:
    html = r'''
    {"dehydratedAt":1,"state":{"data":{"openPrice":11111,"closePrice":11112},"status":"success"},"queryKey":["crypto-prices","price","BTC","2026-07-11T03:05:00Z","fiveminute","2026-07-11T03:10:00Z"]}
    {"dehydratedAt":2,"state":{"data":null,"status":"pending"},"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:15:00Z"]}
    '''

    prices = parse_polymarket_outcome_prices(html)

    assert [price.slug for price in prices] == ["btc-updown-5m-1783739100"]


def test_outcome_parser_rejects_conflicting_duplicates_for_same_interval() -> None:
    html = r'''
    {"dehydratedAt":1,"state":{"data":{"openPrice":64000,"closePrice":null},"status":"success"},"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:15:00Z"]}
    {"dehydratedAt":2,"state":{"data":{"openPrice":64005,"closePrice":null},"status":"success"},"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:15:00Z"]}
    '''

    assert parse_polymarket_outcome_prices(html) == []


def test_outcome_parser_requires_exact_btc_five_minute_query() -> None:
    html = r'''
    {"state":{"data":{"openPrice":64000,"closePrice":null},"status":"success"},"queryKey":["crypto-prices","price","ETH","2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:15:00Z"]}
    {"state":{"data":{"openPrice":64001,"closePrice":null},"status":"success"},"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:16:00Z"]}
    {"state":{"data":{"openPrice":64002,"closePrice":null},"status":"success"},"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z","oneminute","2026-07-11T03:11:00Z"]}
    '''

    assert parse_polymarket_outcome_prices(html) == []


def test_outcome_parser_ignores_slug_data_fallback_without_exact_query_key() -> None:
    html = r'''
    {"slug":"btc-updown-5m-1783739400","other":{"data":{"openPrice":12345,"closePrice":null}}}
    '''

    assert parse_polymarket_outcome_prices(html) == []


def test_outcome_parser_does_not_pair_unknown_wrapper_with_previous_state() -> None:
    html = r'''
    {"state":{"data":{"openPrice":11111,"closePrice":null},"status":"success"},"foo":1}
    {"wrapper":true,"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:15:00Z"]}
    '''

    assert parse_polymarket_outcome_prices(html) == []


def test_outcome_parser_only_reads_top_level_query_key() -> None:
    html = r'''
    {"state":{"data":{"openPrice":22222,"closePrice":null},"status":"success"},"metadata":{"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:15:00Z"]},"queryKey":["unrelated"]}
    '''

    assert parse_polymarket_outcome_prices(html) == []


def test_outcome_parser_rejects_conflicting_close_and_nonfinite_prices() -> None:
    conflicting = r'''
    {"dehydratedAt":1,"state":{"data":{"openPrice":64000,"closePrice":64001},"status":"success"},"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:15:00Z"]}
    {"dehydratedAt":2,"state":{"data":{"openPrice":64000,"closePrice":64002},"status":"success"},"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:15:00Z"]}
    '''
    nonfinite = r'''
    {"state":{"data":{"openPrice":NaN,"closePrice":null},"status":"success"},"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:15:00Z"]}
    '''

    assert parse_polymarket_outcome_prices(conflicting) == []
    assert parse_polymarket_outcome_prices(nonfinite) == []


def test_market_page_data_requires_previous_market_page_for_previous_close() -> None:
    start = datetime(2026, 7, 11, 3, 10, tzinfo=timezone.utc)
    slug = f"btc-updown-5m-{int(start.timestamp())}"
    current_html = (
        '{"state":{"data":{"openPrice":64001,"closePrice":null},"status":"success"},'
        '"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z",'
        '"fiveminute","2026-07-11T03:15:00Z"]}'
        '{"results":[{"startTime":"2026-07-11T03:05:00Z",'
        '"endTime":"2026-07-11T03:10:00Z","openPrice":64000,"closePrice":64001}]}'
    )

    class PreviousPageFailureClient(PolymarketClient):
        async def event_page_text(self, market_slug: str, timeout: float | None = None) -> str:
            if market_slug == slug:
                return current_html
            raise OSError("previous page unavailable")

    outcome, results = asyncio.run(PreviousPageFailureClient(SourceConfig()).market_page_data(slug))

    assert outcome is not None and outcome.open_price == 64001
    assert [result for result in results if result.end_time == start] == []


def test_outcome_parser_decodes_complete_next_flight_object() -> None:
    query_object = (
        '{"dehydratedAt":1,"state":{"data":{"openPrice":64001,"closePrice":null},'
        '"status":"success"},"queryKey":["crypto-prices","price","BTC",'
        '"2026-07-11T03:10:00Z","fiveminute","2026-07-11T03:15:00Z"]}'
    )
    html = f"<script>self.__next_f.push({json.dumps([1, query_object])})</script>"

    prices = parse_polymarket_outcome_prices(html)

    assert len(prices) == 1
    assert prices[0].open_price == 64001


def test_outcome_parser_never_pairs_state_and_query_key_across_next_flight_chunks() -> None:
    state_only = '{"state":{"data":{"openPrice":64001,"closePrice":null},"status":"success"}}'
    query_only = (
        '{"queryKey":["crypto-prices","price","BTC","2026-07-11T03:10:00Z",'
        '"fiveminute","2026-07-11T03:15:00Z"]}'
    )
    html = (
        f"<script>self.__next_f.push({json.dumps([1, state_only])})</script>"
        f"<script>self.__next_f.push({json.dumps([1, query_only])})</script>"
    )

    assert parse_polymarket_outcome_prices(html) == []
