"""
Example: How to initialize SportsmansGuideScraper with browser mode setting in engine.py

Add this to your engine.py where you initialize the SG scraper:
"""

# At the top of engine.py, import the setting:
from config.settings import SG_USE_PHYSICAL_BROWSER
from scrapers.sportsmans_guide import SportsmansGuideScraper

# When initializing the scraper for a row:
def process_sg_url(url, variation, browser_manager, log_func):
    """
    Initialize SG scraper with the physical browser setting.
    """
    # Create scraper with setting
    scraper = SportsmansGuideScraper(
        browser_manager=browser_manager,
        logger_func=log_func,
        use_physical_browser=SG_USE_PHYSICAL_BROWSER  # Use the setting
    )
    
    # Scrape the URL
    status, price, stock, title, variants, logs = scraper.scrape(url, variation)
    
    return status, price, stock, title, variants, logs


# ============================================================================
# ALTERNATIVE: Make the setting dynamic (from UI)
# ============================================================================
# If you want to toggle it from the UI without restarting:

def process_sg_url_dynamic(url, variation, browser_manager, log_func, use_physical=True):
    """
    Initialize SG scraper with dynamic browser mode (from UI).
    """
    scraper = SportsmansGuideScraper(
        browser_manager=browser_manager,
        logger_func=log_func,
        use_physical_browser=use_physical  # Passed from UI
    )
    
    status, price, stock, title, variants, logs = scraper.scrape(url, variation)
    
    return status, price, stock, title, variants, logs

