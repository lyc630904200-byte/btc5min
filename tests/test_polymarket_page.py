from datetime import datetime, timezone

from polybtc.clients import parse_polymarket_outcome_prices, parse_polymarket_past_results


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
