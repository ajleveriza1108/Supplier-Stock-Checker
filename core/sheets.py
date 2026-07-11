import sys
import time
import random
import copy
import gspread
from google.oauth2.service_account import Credentials
import requests
import tkinter as tk
from tkinter import messagebox

# Import the settings we created earlier
from config.settings import SPREADSHEET_URL, SHEET_NAME, SCOPES, SERVICE_ACCOUNT_FILE

class GoogleSheetsManager:
    def __init__(self, logger_func=None):
        self.log = logger_func 
        self.sheet = self._initialize_sheet()

    def _initialize_sheet(self):
        try:
            creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
            client = gspread.authorize(creds)
            return client.open_by_url(SPREADSHEET_URL).worksheet(SHEET_NAME)
        except Exception as e:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Google Sheets Error", 
                f"Failed to initialize Google Sheets: {str(e)}.\nEnsure '{SERVICE_ACCOUNT_FILE}' is present and valid."
            )
            sys.exit(1)

    def get_all_rows(self):
        """Fetches all data from the sheet with robust retry logic."""
        def _execute():
            return self.sheet.get_all_values()
            
        try:
            return self.retry_with_backoff(_execute)
        except Exception as e:
            if self.log:
                self.log(f"Failed to fetch Google Sheet data after retries: {str(e)}", "error")
            return []

    def batch_update(self, updates):
        """Executes a batch update with retry logic."""
        def _execute():
            safe_updates = copy.deepcopy(updates)
            # CRITICAL FIX: By using USER_ENTERED, we prevent Google Sheets from prepending 
            # a single quote (') to prices. It forces the sheet to format the text into a real currency number.
            self.sheet.batch_update(safe_updates, value_input_option='USER_ENTERED')
        self.retry_with_backoff(_execute)

    def update_cell(self, row, col, value):
        """Updates a single cell with retry logic."""
        def _execute():
            self.sheet.update_cell(row, col, value)
        self.retry_with_backoff(_execute)

    def retry_with_backoff(self, func, max_retries=5, initial_delay=2.0):
        """
        Handles Google API Rate Limits (429) AND raw network disconnects automatically.
        Uses Exponential Backoff with Jitter to prevent multi-threading crashes.
        """
        delay = initial_delay
        
        for attempt in range(max_retries):
            try:
                return func()
            except Exception as e:
                error_str = str(e).lower()
                
                # Check if it's an API rate limit OR a raw network disconnection/timeout
                is_retryable = any(keyword in error_str for keyword in [
                    '429', '500', '502', '503', '504', 
                    'timeout', 'connection', 'disconnected', 'aborted'
                ])
                
                if (is_retryable or isinstance(e, requests.exceptions.RequestException)) and attempt < max_retries - 1:
                    sleep_time = delay + random.uniform(0.1, 1.5)
                    
                    if self.log:
                        self.log(f"Google API/Network issue hit. Retrying in {sleep_time:.1f}s... (Attempt {attempt + 1}/{max_retries})", "error")
                    
                    time.sleep(sleep_time)
                    delay *= 2 
                else:
                    if self.log:
                        self.log(f"Google Sheets operation failed permanently after {attempt + 1} attempts: {str(e)}", "error")
                    raise e