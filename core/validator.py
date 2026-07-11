from smart_tools import OllamaAI

class SuspicionValidator:
    """
    Acts as the middleman between the ScrapingEngine and the AI.
    Holds items that flagged as suspicious in Phase 1 and processes them in Phase 2.
    """
    def __init__(self):
        self.pending_items = []
        self.pass1_data = {}

    def flag(self, item_dict: dict):
        """
        Adds a suspicious item to the pending queue for Phase 2 verification.
        Expected keys: supplier, url, row, row_num, target_var, pass1_price, pass1_stock
        """
        self.pending_items.append(item_dict)

    def has_pending(self) -> bool:
        """Checks if there are items waiting for Phase 2 AI verification."""
        return len(self.pending_items) > 0

    def pop_pending(self) -> list:
        """Extracts and clears the current pending queue for processing."""
        items = self.pending_items.copy()
        self.pending_items.clear()
        return items

    def verify(self, row_num: int, url: str, price: str, stock: str, ai_mode: str, supplier: str = "") -> bool:
        """
        Phase 2: Calls the local LLM to manually read the webpage and verify Phase 1's conclusion.
        Uses the 'supplier' tag to load dynamic rules from ai_prompts.json.
        
        Returns:
            True if AI confirms the scraper is correct.
            False if AI overturns the scraper (False Positive).
        """
        try:
            # Initialize the AI with the user's selected model
            ai = OllamaAI(model_string=ai_mode)
            
            # Pass the URL, Regex Price, Regex Stock, AND the Supplier tag to the AI
            is_correct = ai.verify_scrape(url, price, stock, supplier=supplier)
            
            return is_correct
            
        except Exception as e:
            print(f"Validator Error during AI verification: {e}")
            # If the AI completely crashes, we default to True so we don't lose the Phase 1 data.
            return True