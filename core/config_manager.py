import os
import json
from typing import Dict, Any
from dotenv import load_dotenv

class ConfigManager:
    """Handles application settings, state, and environment variables."""
    
    def __init__(self, config_file: str = "app_config.json"):
        self.config_file = config_file
        
        # Load secrets from .env file
        load_dotenv()
        self.service_account_file: str = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
        self.spreadsheet_url: str = os.getenv("SPREADSHEET_URL", "https://docs.google.com/spreadsheets/d/1KflGzPLIszDAxyiaoMJBOkmI_7JAmow2fFB2s_-H56c/edit")
        
        self.app_state: Dict[str, Any] = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"start_row": "2"}

    def save_config(self, key: str, value: Any) -> None:
        self.app_state[key] = value
        try:
            with open(self.config_file, "w") as f:
                json.dump(self.app_state, f)
        except Exception:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.app_state.get(key, default)