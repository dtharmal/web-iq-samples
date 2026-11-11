# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import io
from pathlib import Path

import pytest

from webiq_vendor_questionnaire import cli
from webiq_vendor_questionnaire.client import Page

SAMPLE_DIR = Path(__file__).resolve().parents[1]


class Terminal(io.StringIO):
    def isatty(self):
        return True


class FakeBrowseClient:
    def __init__(self, api_key):
        self.api_key = api_key

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def browse(self, url, query, *, live_crawl=False):
        return Page(url, "Synthetic page", "Customer data is stored in the region you choose.", None, None)


def test_help_needs_no_key(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "--live-crawl" in capsys.readouterr().out


def test_refuses_redirected_output(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdout", io.StringIO())
    monkeypatch.setenv("WEBIQ_API_KEY", "k")
    assert cli.main([]) == 1
    assert "interactive terminal" in capsys.readouterr().err


def test_missing_key_stops_before_any_call(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdout", Terminal())
    monkeypatch.delenv("WEBIQ_API_KEY", raising=False)
    assert cli.main([]) == 1
    assert "WEBIQ_API_KEY" in capsys.readouterr().err


def test_run_with_the_checked_in_files(monkeypatch):
    terminal = Terminal()
    monkeypatch.setattr(cli.sys, "stdout", terminal)
    monkeypatch.chdir(SAMPLE_DIR)
    monkeypatch.setenv("WEBIQ_API_KEY", "k")
    monkeypatch.setattr(cli, "BrowseClient", FakeBrowseClient)
    assert cli.main([]) == 0
    text = terminal.getvalue()
    assert "Vendor: Microsoft" in text
    assert "[1] Where is customer data stored" in text
    assert "Nothing was saved." in text
