"""Wallet Intelligence in a real browser: CSP-clean render, sorting, filters, mobile layout."""

# ruff: noqa: F811
from __future__ import annotations

import re

import pytest
from test_dashboard_db import PUB1, PUB2, seeded  # noqa: F401
from test_wallet_intel import board  # noqa: F401
from test_web import make_live  # noqa: F401

pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def open_page(browser, lv, path, width=1280):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    page.goto(f"{lv.base}{path}")
    return ctx, page


def test_leaderboard_renders_sorts_filters_and_exports(board, make_live, browser):
    lv = make_live()
    ctx, page = open_page(browser, lv, "/leaderboard?window=all")
    try:
        page.wait_for_selector("table tbody tr")
        assert page.locator("table tbody tr").count() == 4
        page.click("th a:text-is('Agora PnL')")
        page.wait_for_url(re.compile(r"sort=pnl"))
        assert page.locator("th[aria-sort] a, th a[aria-sort]").count() >= 1
        page.select_option("select[name=membership]", "former")
        page.click("#intel-form button[type=submit]")
        page.wait_for_url(re.compile(r"membership=former"))
        assert page.locator("table tbody tr").count() == 2
        href = page.get_attribute("a:text-is('Export CSV')", "href")
        assert href.startswith("/api/v1/leaderboard.csv") and "membership=former" in href
        page.click("a.btn:text-is('Reset')")
        page.wait_for_url(re.compile(r"/leaderboard$"))
        assert page.errors == []
    finally:
        ctx.close()


def test_wallet_profile_and_mobile_layout(board, make_live, browser):
    lv = make_live()
    ctx, page = open_page(browser, lv, f"/leaderboard/{PUB1}", width=390)
    try:
        page.wait_for_selector("h1")
        assert "Rank history" in page.inner_text("body")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert page.errors == []
    finally:
        ctx.close()
    ctx, page = open_page(browser, lv, "/leaderboard?window=all", width=390)
    try:
        page.wait_for_selector("table")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    finally:
        ctx.close()
