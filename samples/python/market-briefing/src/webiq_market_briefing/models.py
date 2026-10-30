# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from dataclasses import dataclass


@dataclass(frozen=True)
class Evidence:
    title: str
    url: str
    content: str
    query: str
    result_type: str
    publisher: str | None = None
    updated_at: str | None = None
