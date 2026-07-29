from __future__ import annotations

import json
import time
from pathlib import Path

import scrapers.walmart_scrapling as scrapling_module
from core.scraper_diagnostics import read_structured_result
from scrapers.walmart_scrapling import WalmartScraplingScraper


ITEM_ID = "746021606"
URL = f"https://www.walmart.com/ip/Test-Product/{ITEM_ID}"


def _snapshot(*, item_id: str = ITEM_ID, price: str = "$98.00", captcha: bool = False):
    return {
        "pageUrl": f"https://www.walmart.com/ip/Test-Product/{item_id}",
        "pageItemId": item_id,
        "identityIds": [item_id],
        "identityConflict": False,
        "title": "Expert Grill Heavy Duty Charcoal Grill",
        "captcha": captcha,
        "enabledPurchase": not captcha,
        "purchaseControlExact": True,
        "purchaseControl": {},
        "priceText": price if not captcha else "",
        "priceExact": True,
        "priceConflict": False,
        "lowStockTexts": [],
        "lowStockExact": True,
        "selectedOptionOos": False,
        "selectedOptionExact": True,
        "productOos": False,
        "productOosExact": True,
        "productOosTexts": [],
        "fulfillment": {"shipping": "Shipping Arrives tomorrow"},
        "allFulfillmentUnavailable": False,
        "jsonLdExact": {},
        "jsonLdConflict": False,
        "locationText": "",
    }


class FakePage:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.index = 0
        self.url = URL
        self._closed = False

    def wait_for_selector(self, *_args, **_kwargs):
        return None

    def evaluate(self, script, requested_item_id):
        assert "requestedItemIdArg" in script
        assert requested_item_id == ITEM_ID
        value = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return value

    def is_closed(self):
        return self._closed


class FakeFetcher:
    calls = []
    snapshots = [_snapshot(), _snapshot()]

    @classmethod
    def fetch(cls, url, **kwargs):
        cls.calls.append((url, dict(kwargs)))
        page = FakePage(cls.snapshots)
        action = kwargs.get("page_action")
        if action:
            action(page)
        return object()


def _make_scraper(tmp_path: Path, *, headless: bool = True):
    config = {
        "navigation_timeout_ms": 30000,
        "settle_timeout_ms": 5000,
        "snapshot_gap_ms": 600,
        "scrapling_max_snapshots": 2,
        "scrapling_profile_directory": str(tmp_path / "profile"),
        "scrapling_solve_cloudflare": True,
        "scrapling_manual_timeout_seconds": 60,
        "scrapling_fallback_to_selenium": False,
    }
    path = tmp_path / "walmart_playwright.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return WalmartScraplingScraper(headless=headless, config_path=path)


def test_headless_toggle_is_forwarded_to_scrapling(tmp_path, monkeypatch):
    FakeFetcher.calls = []
    FakeFetcher.snapshots = [_snapshot(), _snapshot()]
    monkeypatch.setattr(scrapling_module, "StealthyFetcher", FakeFetcher)
    scraper = _make_scraper(tmp_path, headless=True)

    result = scraper.scrape(URL)
    assert result[0] == "Success"
    assert FakeFetcher.calls[-1][1]["headless"] is True

    scraper.set_headless(False)
    result = scraper.scrape(URL)
    assert result[0] == "Success"
    assert FakeFetcher.calls[-1][1]["headless"] is False
    assert scraper.mode_label == "Physical browser (visible)"


def test_same_persistent_profile_is_used_in_both_modes(tmp_path, monkeypatch):
    FakeFetcher.calls = []
    FakeFetcher.snapshots = [_snapshot(), _snapshot()]
    monkeypatch.setattr(scrapling_module, "StealthyFetcher", FakeFetcher)
    scraper = _make_scraper(tmp_path, headless=True)
    scraper.scrape(URL)
    first_profile = FakeFetcher.calls[-1][1]["user_data_dir"]
    scraper.set_headless(False)
    scraper.scrape(URL)
    second_profile = FakeFetcher.calls[-1][1]["user_data_dir"]
    assert first_profile == second_profile
    assert Path(first_profile).exists()


def test_walmart_price_is_internal_only(tmp_path, monkeypatch):
    FakeFetcher.calls = []
    FakeFetcher.snapshots = [_snapshot(price="$98.00"), _snapshot(price="$98.00")]
    monkeypatch.setattr(scrapling_module, "StealthyFetcher", FakeFetcher)
    scraper = _make_scraper(tmp_path)
    status, price, stock, _title, variants, _logs = scraper.scrape(URL)
    assert status == "Success"
    assert stock == "In Stock"
    assert price == ""
    assert variants[0]["price"] == ""
    structured = read_structured_result(variants)
    assert structured is not None
    assert str(structured.price) == "98.00"
    assert structured.metadata["browser_engine"] == "scrapling"


def test_wrong_item_never_becomes_success(tmp_path, monkeypatch):
    FakeFetcher.calls = []
    FakeFetcher.snapshots = [
        _snapshot(item_id="803538514"),
        _snapshot(item_id="803538514"),
    ]
    monkeypatch.setattr(scrapling_module, "StealthyFetcher", FakeFetcher)
    scraper = _make_scraper(tmp_path)
    result = scraper.scrape(URL)
    assert result[0] in {"Error", "Review"}
    assert result[1] == ""


def test_captcha_is_blocked_and_never_updates(tmp_path, monkeypatch):
    FakeFetcher.calls = []
    FakeFetcher.snapshots = [_snapshot(captcha=True)]
    monkeypatch.setattr(scrapling_module, "StealthyFetcher", FakeFetcher)
    scraper = _make_scraper(tmp_path)
    status, price, stock, _title, variants, _logs = scraper.scrape(URL)
    assert status == "Error"
    assert price == ""
    assert stock == "Captcha/Blocked"
    structured = read_structured_result(variants)
    assert structured is not None
    assert structured.verification.value == "BLOCKED"


def test_missing_scrapling_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(scrapling_module, "StealthyFetcher", None)
    scraper = _make_scraper(tmp_path)
    status, price, stock, _title, _variants, _logs = scraper.scrape(URL)
    assert status == "Error"
    assert price == ""
    assert stock == "UNKNOWN"


def test_manual_verification_uses_visible_same_profile(tmp_path, monkeypatch):
    class ManualFetcher(FakeFetcher):
        @classmethod
        def fetch(cls, url, **kwargs):
            cls.calls.append((url, dict(kwargs)))
            # Do not execute the five-minute hold action in this unit test.
            return object()

    ManualFetcher.calls = []
    monkeypatch.setattr(scrapling_module, "StealthyFetcher", ManualFetcher)
    scraper = _make_scraper(tmp_path)
    opened, _message = scraper.open_manual_verification(URL)
    assert opened is True
    deadline = time.monotonic() + 2
    while not ManualFetcher.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ManualFetcher.calls
    kwargs = ManualFetcher.calls[-1][1]
    assert kwargs["headless"] is False
    assert kwargs["user_data_dir"] == str(scraper.profile_dir)
