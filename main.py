import os
import sys
import subprocess
import shutil
import socket

# =========================================================================
# SUPER ROBUST IMPORT PATH FIX - Runs FIRST before anything else
# This permanently fixes "No module named 'scrapers.base_scraper'"
# =========================================================================
def fix_import_paths():
    # Get current file location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = script_dir

    # Multiple possible locations (covers running from root, subfolders, or shortcuts)
    possible_paths = [
        project_root,
        os.path.abspath(os.path.join(script_dir, '..')),
        os.path.abspath(os.path.join(script_dir, '../..')),
        os.path.join(project_root, "scrapers"),
        os.path.join(project_root, "core"),
        os.path.join(project_root, "ui"),
    ]

    for p in possible_paths:
        if p and p not in sys.path:
            sys.path.insert(0, p)

    # Force Python to recognize the scrapers package
    if 'scrapers' not in sys.modules:
        try:
            import scrapers
        except ImportError:
            pass

    # Print for debugging (only once)
    if not hasattr(fix_import_paths, "already_logged"):
        print(f"✅ Import paths fixed. Project root: {project_root}")
        fix_import_paths.already_logged = True

# Run the fix immediately
fix_import_paths()
# =========================================================================

def set_working_directory():
    """
    Ensures the script's working directory is the project root.
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)
    
    # Extra safety - already done in fix_import_paths, but keep original logic
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    
    return project_root

def ensure_venv(project_root):
    """
    Checks if the script is running inside a Virtual Environment.
    If not, it automatically creates one and relaunches the script inside it.
    """
    in_venv = sys.prefix != sys.base_prefix
    if in_venv:
        return 

    print("No virtual environment detected. Initializing Auto-VENV Launcher...")
    
    hostname = socket.gethostname()
    venv_name = f".venv_{hostname}"
    venv_dir = os.path.join(project_root, venv_name)
    
    if sys.platform == "win32":
        venv_python = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        venv_python = os.path.join(venv_dir, "bin", "python")

    if os.path.exists(venv_python):
        try:
            subprocess.check_call([venv_python, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, OSError):
            print(f"Detected broken virtual environment. Rebuilding for {hostname}...")
            shutil.rmtree(venv_dir, ignore_errors=True)

    if not os.path.exists(venv_dir):
        print(f"Creating local virtual environment ({venv_name})... This may take a moment.")
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
        
    if not os.path.exists(venv_python):
        print(f"Error: Could not find venv Python at {venv_python}")
        sys.exit(1)

    print("Relaunching application safely within the virtual environment...")
    os.execl(venv_python, venv_python, *sys.argv)

def main():
    try:
        # 1. Fix import paths immediately
        fix_import_paths()

        # 2. Normalize working directory
        project_root = set_working_directory()

        # 3. ENFORCE VENV
        ensure_venv(project_root)

        # 4. Import installer (safe now)
        from core.dependencies import install_dependencies

        # 5. Run installer only once
        hostname = socket.gethostname()
        setup_marker = os.path.join(project_root, f".venv_{hostname}", ".setup_complete")
        
        if not os.path.exists(setup_marker):
            print("First run detected: Checking and installing dependencies...")
            install_dependencies()
            with open(setup_marker, "w") as f:
                f.write("Dependencies installed successfully.")
        
        # 6. Import and run the main app
        from ui.app import StockPriceCheckerApp
        
        app = StockPriceCheckerApp()
        app.mainloop()
        
    except KeyboardInterrupt:
        print("\nApplication closed by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\nFatal Error during startup: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()