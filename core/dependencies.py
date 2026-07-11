import subprocess
import sys
import tkinter as tk
from tkinter import messagebox
import os

def install_dependencies() -> None:
    """Installs dependencies, applies shims, and installs Playwright browsers."""
    
    required_packages = [
        'setuptools<70.0.0', 
        'requests',
        'beautifulsoup4',
        'gspread',
        'google-auth',
        'cloudscraper',
        'selenium',
        'webdriver-manager',
        'plyer',
        'lxml',
        'customtkinter',
        'python-dotenv',
        'undetected-chromedriver',
        'scrapling',      
        'playwright',
        'price-parser'    # NEW: Robust price extraction library
    ]
    
    print("Executing Emergency Dependency Sync...")
    try:
        # 1. Standard Install
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip', '--quiet'])
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--upgrade'] + required_packages)
        
        # 2. Install Playwright Chromium binaries (Required for Scrapling)
        print("Installing Playwright Chromium engine for Scrapling...")
        subprocess.check_call([sys.executable, '-m', 'playwright', 'install', 'chromium'])

        # 3. THE EMERGENCY SHIM: 
        # We manually tell the virtual environment that 'distutils' is just 'setuptools._distutils'
        venv_site_packages = next(p for p in sys.path if 'site-packages' in p)
        shim_path = os.path.join(venv_site_packages, 'distutils-precedence.pth')
        
        with open(shim_path, 'w') as f:
            f.write("import os; import sys; import setuptools; sys.modules['distutils'] = setuptools._distutils")
            
        print("Shielding complete. Environment stabilized.")
        
    except Exception as e:
        root = tk.Tk()
        root.withdraw() 
        messagebox.showerror("Sync Error", f"Critical failure: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    install_dependencies()