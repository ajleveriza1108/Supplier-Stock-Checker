import os
import sys
import time
import subprocess
import threading
import requests
import glob
from typing import Tuple, List

class SurfsharkVPN:
    def __init__(self):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.vpn_dir = os.path.join(self.base_dir, "vpn")
        self.auth_path = os.path.join(self.vpn_dir, "auth.txt")
        
        # Send logs directly to a file to prevent memory buffer deadlocks
        self.log_path = os.path.join(self.vpn_dir, "vpn_log.txt") 
        
        self.openvpn_exe = r"C:\Program Files\OpenVPN\bin\openvpn.exe"
        self.process = None
        
        # Load and sort all available VPN configs
        self.configs = self._get_sorted_configs()
        self.current_index = 0

    def _get_sorted_configs(self) -> List[str]:
        """
        Finds all .ovpn files in the vpn directory and sorts them intelligently:
        1. Prioritize 'sea' (Seattle) UDP
        2. Prioritize 'sea' (Seattle) TCP
        3. Prioritize other UDP
        4. Prioritize other TCP
        """
        if not os.path.exists(self.vpn_dir):
            return []

        all_configs = glob.glob(os.path.join(self.vpn_dir, "*.ovpn"))
        if not all_configs:
            return []

        def sort_key(filepath):
            filename = os.path.basename(filepath).lower()
            
            if "sea" in filename and "udp" in filename: return 0
            elif "sea" in filename and "tcp" in filename: return 1
            elif "udp" in filename: return 2
            elif "tcp" in filename: return 3
            else: return 4

        return sorted(all_configs, key=sort_key)

    def _kill_zombies(self):
        """Forcefully kills any lingering background OpenVPN processes and scrubs zombie routes."""
        if sys.platform == "win32":
            os.system("taskkill /F /IM openvpn.exe >nul 2>&1")
            
            # CRITICAL FIX: Delete the lingering 'def1' routes from the Windows Routing Table
            # that are left behind when OpenVPN is forcefully closed or crashes.
            os.system("route delete 0.0.0.0 mask 128.0.0.0 >nul 2>&1")
            os.system("route delete 128.0.0.0 mask 128.0.0.0 >nul 2>&1")

    def _read_log(self) -> str:
        """Safely reads the external log file."""
        if os.path.exists(self.log_path):
            try:
                with open(self.log_path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read().strip()
            except Exception:
                return "Could not read log file."
        return "No log file generated."

    def connect(self) -> Tuple[bool, str]:
        """Initiates the OpenVPN connection with forced routing, Smart Polling, and config rotation."""
        if not self.configs:
            return False, f"Config missing: Please place .ovpn files in the '{self.vpn_dir}' folder."
        if not os.path.exists(self.openvpn_exe):
            return False, f"OpenVPN not found at {self.openvpn_exe}. Please install OpenVPN GUI."
        if not os.path.exists(self.auth_path):
            return False, f"Credentials missing: Please create 'auth.txt' in the '{self.vpn_dir}' folder."

        # Try up to 3 different servers before giving up completely
        attempts = min(3, len(self.configs))
        
        for attempt_num in range(attempts):
            config_path = self.configs[self.current_index]
            config_name = os.path.basename(config_path)
            
            if self.process:
                 self.disconnect()
                
            self._kill_zombies()
            
            # Clear the old log file to prevent it from getting massive
            if os.path.exists(self.log_path):
                try:
                    os.remove(self.log_path)
                except Exception:
                    pass

            cmd = [
                self.openvpn_exe,
                "--config", config_path,
                "--auth-user-pass", self.auth_path,
                "--auth-nocache",
                "--mute-replay-warnings",
                "--redirect-gateway", "def1",  # Force Windows to route ALL traffic through the VPN
                "--block-outside-dns",         # Prevent Windows from leaking Philippine DNS requests
                "--log", self.log_path         # Route output to file, not memory
            ]
            
            try:
                creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                
                # Using DEVNULL prevents the OS pipe buffer from filling up and freezing the app
                self.process = subprocess.Popen(
                    cmd, 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.DEVNULL,
                    creationflags=creation_flags
                )
                
                time.sleep(2)
                if self.process.poll() is not None:
                    error_log = self._read_log()
                    self.current_index = (self.current_index + 1) % len(self.configs)
                    if attempt_num == attempts - 1:
                         return False, f"OpenVPN crashed immediately on {config_name}. Error: {error_log}"
                    continue # Try next config

                # --- SMART POLLING IP VERIFICATION ---
                max_attempts = 15 # Expanded to 45 seconds to account for slower TCP handshakes
                for attempt in range(max_attempts):
                    time.sleep(3) 
                    
                    if self.process.poll() is not None:
                        break # Process died, break out of polling loop and try next config
                    
                    try:
                        resp = requests.get("https://ipinfo.io/json", timeout=5)
                        data = resp.json()
                        country = data.get("country", "")
                        ip = data.get("ip", "")
                        
                        if country != "PH" and country != "":
                            return True, f"Surfshark VPN Verified Securely: {ip} ({country}) via {config_name}"
                    except Exception:
                        pass 
                
                # --- FAILURE DIAGNOSTICS (If we exit the polling loop without returning True) ---
                self.process.terminate()
                self.disconnect()
                
                # Rotate to the next config in the list
                self.current_index = (self.current_index + 1) % len(self.configs)
                time.sleep(2)
                continue # Loop restarts with the next config
                
            except Exception as e:
                self.current_index = (self.current_index + 1) % len(self.configs)
                if attempt_num == attempts - 1:
                    return False, f"Failed to start VPN: {str(e)}"
                continue

        log_dump = self._read_log()
        tail_log = log_dump[-500:] if len(log_dump) > 500 else log_dump
        return False, f"VPN LEAK DETECTED! Routing failed after {attempts} attempts. Last config: {config_name}. Log: ...{tail_log}"

    def disconnect(self):
        """Terminates the OpenVPN process and restores normal networking."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
            finally:
                self.process = None
                
        self._kill_zombies()
        time.sleep(3)