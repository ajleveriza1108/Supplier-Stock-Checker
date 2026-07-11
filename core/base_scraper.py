import abc
import time
from typing import Tuple, List, Dict, Any


class BaseScraper(abc.ABC):
    """
    Abstract base class for all scrapers.
    """

    def __init__(self, browser_manager=None, logger_func=None):
        self.browser_manager = browser_manager
        self.log = logger_func or (lambda msg, tag="info": None)  # silent by default

    @abc.abstractmethod
    def scrape(self, url: str, target_variation: str = "") -> Tuple[str, str, str, str, List[Dict], List]:
        pass

    def get_driver(self):
        if self.browser_manager:
            return self.browser_manager.get_driver()
        from selenium import webdriver
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        return webdriver.Chrome(options=options)

    def wait_for_js_interactive(self, timeout=10):
        if self.browser_manager:
            self.browser_manager.wait_for_js_interactive(timeout)
        else:
            time.sleep(3)