"""
Tkinter UI Code: Add checkbox for SG browser mode selection

Add this to your ui/app.py (in the settings/options panel)
"""

import tkinter as tk
from tkinter import ttk
import config.settings as settings

# ============================================================================
# SETTINGS FRAME (add this to your UI)
# ============================================================================

def create_sg_settings_frame(parent):
    """
    Creates the SportsmansGuide browser mode settings frame.
    
    Add this to your main UI window's settings area.
    """
    settings_frame = ttk.LabelFrame(parent, text="Sportsmans Guide Settings", padding=10)
    settings_frame.pack(fill=tk.X, padx=10, pady=10)
    
    # ── Browser Mode Selection ──────────────────────────────────────────
    browser_mode_frame = ttk.Frame(settings_frame)
    browser_mode_frame.pack(fill=tk.X, pady=10)
    
    ttk.Label(browser_mode_frame, text="Browser Mode:").pack(side=tk.LEFT, padx=5)
    
    # Variable to hold the setting
    sg_use_physical = tk.BooleanVar(value=settings.SG_USE_PHYSICAL_BROWSER)
    
    # Checkbox: "Use Physical Browser"
    physical_browser_check = ttk.Checkbutton(
        browser_mode_frame,
        text="Use Physical Browser (Remote Debugging)",
        variable=sg_use_physical,
        onvalue=True,
        offvalue=False,
        command=lambda: on_sg_browser_mode_changed(sg_use_physical.get())
    )
    physical_browser_check.pack(side=tk.LEFT, padx=5)
    
    # Info label
    info_label = ttk.Label(
        browser_mode_frame,
        text="✓ Physical = Faster, fewer CAPTCHA  |  ☐ Selenium = Self-contained",
        foreground="gray"
    )
    info_label.pack(side=tk.LEFT, padx=20)
    
    return sg_use_physical  # Return the variable so you can read its value later


def on_sg_browser_mode_changed(use_physical):
    """
    Called when the checkbox is toggled.
    Update the setting and log the change.
    """
    settings.SG_USE_PHYSICAL_BROWSER = use_physical
    
    mode_name = "Physical Browser" if use_physical else "Selenium"
    print(f"[Settings] SG Browser Mode changed to: {mode_name}")
    
    # Optionally: show a message to the user
    # messagebox.showinfo("Settings Updated", f"SG will now use: {mode_name}")


# ============================================================================
# EXAMPLE: Full settings panel with multiple options
# ============================================================================

def create_full_settings_panel(parent):
    """
    Example of a complete settings panel with VPN, extraction mode, and browser mode.
    """
    settings_notebook = ttk.Notebook(parent)
    settings_notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    
    # ── Tab 1: General Settings ─────────────────────────────────────────
    general_tab = ttk.Frame(settings_notebook)
    settings_notebook.add(general_tab, text="General")
    
    ttk.Label(general_tab, text="VPN Status: ENABLED").pack(anchor=tk.W, padx=10, pady=5)
    ttk.Label(general_tab, text="Extraction Mode: Regular (Regex)").pack(anchor=tk.W, padx=10, pady=5)
    
    # ── Tab 2: Browser Settings ────────────────────────────────────────
    browser_tab = ttk.Frame(settings_notebook)
    settings_notebook.add(browser_tab, text="Browser Settings")
    
    create_sg_settings_frame(browser_tab)
    
    # ── Tab 3: Advanced ────────────────────────────────────────────────
    advanced_tab = ttk.Frame(settings_notebook)
    settings_notebook.add(advanced_tab, text="Advanced")
    
    ttk.Label(advanced_tab, text="[Other advanced settings here]").pack(padx=10, pady=10)


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    root = tk.Tk()
    root.title("Stock Price Checker - Settings")
    root.geometry("600x300")
    
    sg_browser_var = create_sg_settings_frame(root)
    
    # Button to test the current value
    def test_button():
        value = sg_browser_var.get()
        print(f"Current SG_USE_PHYSICAL_BROWSER = {value}")
    
    ttk.Button(root, text="Test Setting", command=test_button).pack(pady=20)
    
    root.mainloop()

