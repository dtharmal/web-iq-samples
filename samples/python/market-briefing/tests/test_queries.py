# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from datetime import date

from webiq_market_briefing.queries import build_queries


def test_build_queries_includes_topic_and_date_range() -> None:
    queries = build_queries(
        "North American energy market",
        lookback_days=7,
        as_of=date(2026, 10, 6),
    )

    assert len(queries) == 3
    assert all("North American energy market" in query for query in queries)
    assert all("2026-09-29 through 2026-10-06" in query for query in queries)
