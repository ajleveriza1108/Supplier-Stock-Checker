from __future__ import annotations

from pathlib import Path

import pytest

from scrapers.scrapling_browser import (
    EXCLUDED_SUPPLIERS,
    SUPPORTED_SUPPLIERS,
    ScraplingBrowserManager,
    ScraplingElement,
    ScraplingSnapshotDriver,
    _detect_blocked_reason,
    _supplier_code_from_url,
)


def _manager(tmp_path: Path) -> ScraplingBrowserManager:
    return ScraplingBrowserManager(project_root=tmp_path, headless=True)


def test_supported_supplier_scope_excludes_sportsmans_guide() -> None:
    assert SUPPORTED_SUPPLIERS == {"WEB", "WAL", "HF", "MN", "LS", "CE"}
    assert EXCLUDED_SUPPLIERS == {"SG"}


def test_supplier_url_mapping() -> None:
    assert _supplier_code_from_url("https://www.walmart.com/ip/x/123") == "WAL"
    assert _supplier_code_from_url("https://www.webstaurantstore.com/item") == "WEB"
    assert _supplier_code_from_url("https://www.harborfreight.com/tool") == "HF"
    assert _supplier_code_from_url("https://www.menards.com/main/item") == "MN"
    assert _supplier_code_from_url("https://www.lakeside.com/product") == "LS"
    assert _supplier_code_from_url("https://www.collectionsetc.com/product") == "CE"
    assert _supplier_code_from_url("https://www.sportsmansguide.com/product") == "SG"


def test_manager_rejects_sportsmans_guide(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(ValueError):
        manager.for_supplier("SG")


def test_global_headless_toggle_changes_all_scoped_managers(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    scopes = [manager.for_supplier(code) for code in SUPPORTED_SUPPLIERS]
    assert all(scope.headless for scope in scopes)
    manager.set_headless(False)
    assert not any(scope.headless for scope in scopes)


def test_profiles_are_isolated_and_walmart_profile_is_preserved(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    wal = manager.profile_directory("WAL")
    hf = manager.profile_directory("HF")
    ce = manager.profile_directory("CE")
    assert wal.name == "walmart_scrapling"
    assert hf != ce
    assert "browser_profiles" in str(wal)
    assert hf.name == "hf"
    assert ce.name == "ce"


def test_snapshot_driver_reads_rendered_elements_without_selenium_browser(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    driver = ScraplingSnapshotDriver(manager, "HF")
    driver.page_source = '''
    <html><head><title>Tool</title></head><body>
      <button class="add-to-cart" data-testid="add-to-cart">Add to Cart</button>
      <span class="price">$19.99</span>
    </body></html>
    '''
    from bs4 import BeautifulSoup

    driver._soup = BeautifulSoup(driver.page_source, "html.parser")
    items = driver.find_elements("css selector", "button.add-to-cart")
    assert len(items) == 1
    assert isinstance(items[0], ScraplingElement)
    assert items[0].is_displayed()
    assert items[0].is_enabled()
    assert items[0].get_attribute("textContent") == "Add to Cart"
    assert driver.execute_script("return document.readyState") == "complete"


def test_captcha_detection_is_fail_closed() -> None:
    assert _detect_blocked_reason("Pardon Our Interruption", "")
    assert _detect_blocked_reason("Product", "Verify you are human")
    assert not _detect_blocked_reason("Normal Product", "Add to cart $19.99")


def test_installed_app_routes_every_non_sg_supplier_through_scrapling() -> None:
    app_text = Path("ui/app.py").read_text(encoding="utf-8")
    assert "ScraplingBrowserManager" in app_text
    for code in ("WEB", "HF", "MN", "LS", "CE"):
        assert f'self.scrapling_bm.for_supplier("{code}")' in app_text
    assert '"WAL": self.walmart_scraper' in app_text
    assert '"SG": SportsmansGuideScraper(browser_manager=None)' in app_text
    assert 'text="Scrapling Browser: Headless"' in app_text
    assert "Applies to every supplier except Sportsman's Guide" in app_text


def test_harbor_freight_and_lakeside_have_captcha_guards() -> None:
    hf_text = Path("scrapers/harbor_freight.py").read_text(encoding="utf-8")
    ls_text = Path("scrapers/lakeside.py").read_text(encoding="utf-8")
    assert "HF: Scrapling verification/CAPTCHA detected" in hf_text
    assert "Lakeside: Scrapling verification/CAPTCHA detected" in ls_text
