# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from datetime import date, timedelta


def build_queries(topic: str, lookback_days: int, as_of: date | None = None) -> list[str]:
    end_date = as_of or date.today()
    start_date = end_date - timedelta(days=lookback_days)
    date_range = f"{start_date.isoformat()} through {end_date.isoformat()}"

    return [
        f"{topic} major developments {date_range}",
        f"{topic} prices supply demand outlook {date_range}",
        f"{topic} risks regulation disruptions {date_range}",
    ]
