import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox
import webbrowser
import re
from datetime import datetime
from config.settings import SHEET_COLS

class ReviewUpdatesWindow(ctk.CTkToplevel):
    def __init__(self, parent, verified_items, unverified_items, sheets_manager, engine, logger_func):
        super().__init__(parent)
        
        self.parent_app = parent
        self.sheets_manager = sheets_manager
        self.engine = engine
        self.log = logger_func
        
        self.verified_items = verified_items
        self.unverified_items = unverified_items
        
        self.title("Review Pending Updates (Live Monitoring & Edit)")
        self.geometry("1300x650")
        self.transient(parent)
        
        self.row_data_refs = []
        
        self.protocol("WM_DELETE_WINDOW", self.on_window_close)
        
        self._setup_ui()

    def validate_price(self, P):
        """Strictly limits textbox input to numbers, commas, one decimal, and an optional dollar sign."""
        if P == "" or P == "$": 
            return True
        return bool(re.fullmatch(r'^\$?[0-9,]*\.?[0-9]*$', P))

    def format_price_on_focus_out(self, event, string_var):
        """Auto-formats the price to $XX.XX when the user clicks away from the textbox."""
        val = string_var.get().strip().replace('$', '').replace(',', '')
        if not val:
            string_var.set("") 
            return
        try:
            formatted = f"${float(val):.2f}"
            string_var.set(formatted)
        except ValueError: 
            pass 

    def set_all_stock(self, supplier, value):
        """Universal dropdown handler to set stock status for all rows in the active tab."""
        if value == "Set All...": 
            return
        for ref in self.row_data_refs:
            if ref['supplier'] == supplier:
                ref['stock_var'].set(value)

    def clear_all_prices(self, supplier):
        """Universal clear button handler to wipe all price textboxes in the active tab."""
        for ref in self.row_data_refs:
            if ref['supplier'] == supplier:
                ref['price_var'].set("")

    def _get_scraping_status(self):
        """Calculates if the engine is actively scraping or legitimately waiting for Phase 2."""
        is_scraping_active = self.engine.active_threads > 0 or self.engine.is_running
        
        # Only lock the UI if we are in an ACTIVE session, stop was NOT requested, and it's waiting for Phase 2.
        if not is_scraping_active and getattr(self.engine, 'session_timestamp', None) and not self.engine.stop_requested and self.engine.validator.has_pending():
            is_scraping_active = True
            
        return is_scraping_active

    def save_single_note(self, url, note_var, btn_widget):
        """Instantly saves a custom AI directive without needing to push to Sheets."""
        note_text = note_var.get().strip()
        if not note_text:
            messagebox.showinfo("Empty Note", "Please type an AI directive before saving.")
            return

        if hasattr(self.engine.ai_memory, 'add_user_rule'):
            self.engine.ai_memory.add_user_rule(url, note_text)
            self.log(f"✅ Saved custom AI Directive for URL: {url}", "warning")

            # Visual feedback on the button
            original_text = btn_widget.cget("text")
            original_color = btn_widget.cget("fg_color")

            btn_widget.configure(text="Saved!", fg_color="#2FA572") # Flash green
            self.after(2000, lambda: btn_widget.configure(text=original_text, fg_color=original_color))
        else:
            self.log(f"❌ AI Memory Error: Could not save note for {url}. Please update smart_tools.py.", "error")
            messagebox.showerror("Error", "AI Memory module not found.")

    def _setup_ui(self):
        # --- TOP FRAME (Status) ---
        top_frame = ctk.CTkFrame(self, fg_color="transparent")
        top_frame.pack(fill="x", padx=10, pady=10)
        
        is_scraping_active = self._get_scraping_status()

        total_items = len(self.verified_items) + len(self.unverified_items)
        status_text = f"You have {total_items} total flagged items ({len(self.verified_items)} Verified, {len(self.unverified_items)} Awaiting AI Check)."
        
        if is_scraping_active:
            status_text += "\n(Scraping is active. Push is disabled until complete or stopped)."
        elif self.engine.stop_requested and self.unverified_items:
            status_text += "\n(Scrape Stopped. Everything is now unlocked for manual review/push)."
        else:
            status_text += "\nReview and edit the values below. Add custom AI training notes for tricky URLs:"
            
        status_label = ctk.CTkLabel(top_frame, text=status_text, font=ctk.CTkFont(weight="bold"), justify="left")
        status_label.pack(side="left")
        
        # --- TAB VIEW FRAME ---
        self.tabview = ctk.CTkTabview(self, command=self.on_tab_change)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=5)

        display_items = []

        # Load Verified Items
        for item in self.verified_items:
            display_items.append({
                'row_num': item['row_num'],
                'supplier': item['supplier'],
                'old_p': item['prev_price'] or "N/A",
                'new_p': item['price'] or "N/A",
                'old_s': item['prev_stock'] or "N/A",
                'new_s': item['stock'] or "N/A",
                'url': item['url'],
                'status_tag': '[VERIFIED]',
                'raw_item': item,
                'is_verified': True
            })

        # Load Unverified Items
        for item in self.unverified_items:
            if 'row' not in item: 
                item['row'] = []
                
            row = item['row']
            stock_col = SHEET_COLS.get("stock_status", 2)
            price_col = SHEET_COLS.get("price", 15)
            
            old_s = str(row[stock_col]).strip().upper() if len(row) > stock_col else "N/A"
            old_p = str(row[price_col]).strip() if len(row) > price_col else "N/A"
            
            display_items.append({
                'row_num': item['row_num'],
                'supplier': item['supplier'],
                'old_p': old_p or "N/A",
                'new_p': item['pass1_price'] or "N/A",
                'old_s': old_s or "N/A",
                'new_s': item['pass1_stock'] or "N/A",
                'url': item['url'],
                'status_tag': '[PENDING AI]',
                'raw_item': item,
                'is_verified': False
            })

        # Sort items chronologically by row number
        display_items.sort(key=lambda x: x['row_num'])
        suppliers = sorted(list(set(d['supplier'] for d in display_items)))

        # Register the text validator command for the Price textboxes
        vcmd = (self.register(self.validate_price), '%P')

        # Build tabs
        if not suppliers:
            tab = self.tabview.add("No Updates")
            empty_label = ctk.CTkLabel(tab, text="There are no pending updates to display.", font=ctk.CTkFont(size=14))
            empty_label.pack(pady=40)
        else:
            for sup in suppliers:
                tab = self.tabview.add(sup)
                
                header_frame = ctk.CTkFrame(tab, fg_color="#2C3E50", corner_radius=5)
                header_frame.pack(fill="x", pady=(0, 5))
                
                # Standard Headers
                push_label = ctk.CTkLabel(header_frame, text="Push", width=40, font=ctk.CTkFont(weight="bold"))
                push_label.pack(side="left", padx=(15, 5), pady=5)
                
                row_label = ctk.CTkLabel(header_frame, text="Row & Tag", width=150, anchor="w", font=ctk.CTkFont(weight="bold"))
                row_label.pack(side="left", padx=5)
                
                # --- UNIVERSAL STOCK HEADER ---
                stock_header_frame = ctk.CTkFrame(header_frame, width=120, fg_color="transparent")
                stock_header_frame.pack(side="left", padx=5)
                
                stock_col_label = ctk.CTkLabel(stock_header_frame, text="Stock (Col C)", font=ctk.CTkFont(weight="bold", size=12))
                stock_col_label.pack(pady=(2,2))
                
                master_stock_cb = ctk.CTkOptionMenu(
                    stock_header_frame, 
                    values=["In Stock", "OOS", "Blank"], 
                    width=120, 
                    height=20, 
                    font=ctk.CTkFont(size=11), 
                    command=lambda v, s=sup: self.set_all_stock(s, v)
                )
                master_stock_cb.set("Set All...")
                master_stock_cb.pack(pady=(0,4))
                
                # --- UNIVERSAL PRICE HEADER ---
                price_header_frame = ctk.CTkFrame(header_frame, width=100, fg_color="transparent")
                price_header_frame.pack(side="left", padx=5)
                
                price_col_label = ctk.CTkLabel(price_header_frame, text="Price (Col P)", font=ctk.CTkFont(weight="bold", size=12))
                price_col_label.pack(pady=(2,2))
                
                clear_price_btn = ctk.CTkButton(
                    price_header_frame, 
                    text="Clear All", 
                    width=100, 
                    height=20, 
                    font=ctk.CTkFont(size=11), 
                    fg_color="#E74C3C", 
                    hover_color="#C0392B", 
                    command=lambda s=sup: self.clear_all_prices(s)
                )
                clear_price_btn.pack(pady=(0,4))
                
                results_label = ctk.CTkLabel(header_frame, text="Results & AI DirectIVES", anchor="w", font=ctk.CTkFont(weight="bold"))
                results_label.pack(side="left", padx=20, fill="x", expand=True)

                scroll_frame = ctk.CTkScrollableFrame(tab)
                scroll_frame.pack(fill="both", expand=True)

                sup_items = [d for d in display_items if d['supplier'] == sup]
                
                for d in sup_items:
                    row_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
                    row_frame.pack(fill="x", pady=4)
                    
                    # --- Fully enable UI if engine is stopped, even for unverified ---
                    is_active = "normal" if (d['is_verified'] or not is_scraping_active) else "disabled"
                    text_col = "white" if (d['is_verified'] or not is_scraping_active) else "gray"
                    
                    check_var = ctk.BooleanVar(value=d['is_verified'])
                    cb_widget = ctk.CTkCheckBox(row_frame, text="", variable=check_var, width=30, state=is_active)
                    cb_widget.pack(side="left", padx=(15, 5))
                    
                    tag_text = f"Row {d['row_num']:<4}\n{d['status_tag']}"
                    tag_widget = ctk.CTkLabel(row_frame, text=tag_text, width=150, justify="left", anchor="w", text_color=text_col, font=ctk.CTkFont(size=11))
                    tag_widget.pack(side="left", padx=5)
                    
                    # ==========================================
                    # RULE-BASED STOCK DROPDOWN LOGIC
                    # ==========================================
                    old_s_val = str(d['old_s']).strip().upper()
                    new_s_val = str(d['new_s']).strip()
                    
                    old_is_blank = old_s_val in ["N/A", "", "BLANK", "NONE"]
                    
                    if old_is_blank and new_s_val == "OOS":
                        final_stock_selection = "OOS"
                    elif old_s_val == "OOS" and new_s_val == "In Stock":
                        final_stock_selection = "In Stock"
                    elif old_is_blank:
                        final_stock_selection = "Blank"
                    else:
                        final_stock_selection = new_s_val if new_s_val in ["In Stock", "OOS"] else "Blank"
                    
                    stock_var = ctk.StringVar(value=final_stock_selection)
                    dropdown_bg_color = "#34495E" if (d['is_verified'] or not is_scraping_active) else "#2C3E50"
                    
                    stock_widget = ctk.CTkOptionMenu(
                        row_frame, 
                        variable=stock_var, 
                        values=["In Stock", "OOS", "Blank"], 
                        width=120,
                        state=is_active,
                        fg_color=dropdown_bg_color
                    )
                    stock_widget.pack(side="left", padx=5)
                    
                    # ==========================================
                    # STRICT $XX.XX FORMATTING LOAD LOGIC
                    # ==========================================
                    raw_p = d['new_p'] if d['new_p'] != "N/A" else ""
                    formatted_p = ""
                    
                    if raw_p:
                        clean_p = raw_p.replace('$', '').replace(',', '').strip()
                        try:
                            formatted_p = f"${float(clean_p):.2f}"
                        except ValueError:
                            formatted_p = raw_p 
                            
                    price_var = ctk.StringVar(value=formatted_p)
                    price_widget = ctk.CTkEntry(
                        row_frame, 
                        textvariable=price_var, 
                        width=100, 
                        state=is_active, 
                        validate="key", 
                        validatecommand=vcmd
                    )
                    price_widget.pack(side="left", padx=5)
                    price_widget.bind("<FocusOut>", lambda e, var=price_var: self.format_price_on_focus_out(e, var))
                    
                    # ==========================================
                    # PHASE 1 & PHASE 2 INLINE TEXT RESULTS
                    # ==========================================
                    info_frame = ctk.CTkFrame(row_frame, fg_color="transparent")
                    info_frame.pack(side="left", padx=20, fill="x", expand=True)

                    # Sub-frame for status texts
                    text_frame = ctk.CTkFrame(info_frame, fg_color="transparent")
                    text_frame.pack(fill="x")

                    sheet_str = f"Sheet: {d['old_s']} / {d['old_p']}   |   "
                    sheet_label = ctk.CTkLabel(text_frame, text=sheet_str, text_color="gray60", font=ctk.CTkFont(size=11))
                    sheet_label.pack(side="left")
                    
                    if not d['is_verified']:
                        p1_str = f"Phase 1: {d['new_s']} / {d['new_p']}   |   "
                        p2_str = "Phase 2: Pending AI Check..."
                        p2_color = "#F39C12"  
                    else:
                        p1_str = f"Phase 1: {d['new_s']} / {d['new_p']}   |   "
                        ai_status = d['raw_item'].get('ai_status', '')
                        
                        if "Rejected" in ai_status:
                            p2_str = f"Phase 2: AI REJECTED"
                            p2_color = "#E74C3C"  
                        elif "Confirmed" in ai_status:
                            p2_str = f"Phase 2: AI Confirmed"
                            p2_color = "#1ABC9C"  
                        elif "Bypassed" in ai_status or "OFF" in ai_status:
                            p2_str = f"Phase 2: Skipped (AI OFF)"
                            p2_color = "#9B59B6"  
                        else:
                            p2_str = f"Phase 2: Confirmed"
                            p2_color = "#1ABC9C"  
                        
                    p1_label = ctk.CTkLabel(text_frame, text=p1_str, text_color="#3498DB", font=ctk.CTkFont(size=11))
                    p1_label.pack(side="left")
                    
                    p2_label = ctk.CTkLabel(text_frame, text=p2_str, text_color=p2_color, font=ctk.CTkFont(size=11, weight="bold"))
                    p2_label.pack(side="left")
                    
                    # --- NEW: AI MEMORY DIRECTIVE BOX + SAVE BUTTON ---
                    note_container = ctk.CTkFrame(info_frame, fg_color="transparent")
                    note_container.pack(fill="x", pady=(4,0))
                    
                    ai_note_var = ctk.StringVar()
                    ai_note_widget = ctk.CTkEntry(
                        note_container, 
                        textvariable=ai_note_var, 
                        placeholder_text="Provide AI Training Note for this specific URL...", 
                        height=24,
                        font=ctk.CTkFont(size=11)
                    )
                    ai_note_widget.pack(side="left", fill="x", expand=True)
                    
                    save_note_btn = ctk.CTkButton(
                        note_container,
                        text="✔️ Save Note",
                        width=80,
                        height=24,
                        font=ctk.CTkFont(size=11, weight="bold"),
                        fg_color="#8E44AD",
                        hover_color="#732D91"
                    )
                    # We bind the button command dynamically to capture the correct URL and Widget
                    save_note_btn.configure(command=lambda u=d['url'], v=ai_note_var, b=save_note_btn: self.save_single_note(u, v, b))
                    save_note_btn.pack(side="left", padx=(5, 0))
                    
                    link_btn = ctk.CTkButton(
                        row_frame, 
                        text="🔗", 
                        width=40, 
                        height=24, 
                        fg_color="#3498DB", 
                        hover_color="#2980B9",
                        command=lambda u=d['url']: webbrowser.open(u)
                    )
                    link_btn.pack(side="right", padx=10)
                    
                    self.row_data_refs.append({
                        'supplier': sup,
                        'url': d['url'],
                        'row_frame': row_frame,
                        'check_var': check_var,
                        'stock_var': stock_var,
                        'price_var': price_var,
                        'ai_note_var': ai_note_var,
                        'is_verified': d['is_verified'],
                        'raw_item': d['raw_item'],
                        'row_num': d['row_num'],
                        'cb_widget': cb_widget,
                        'tag_widget': tag_widget,
                        'stock_widget': stock_widget,
                        'price_widget': price_widget
                    })

        # --- BOTTOM FRAME (Controls) ---
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.pack(fill="x", padx=10, pady=10)
        
        self.clear_btn = ctk.CTkButton(
            bottom_frame, 
            text="Clear All Data", 
            width=110, 
            fg_color="#E74C3C", 
            hover_color="#C0392B", 
            command=self.clear_all_data
        )
        self.clear_btn.pack(side="left", padx=5)
        
        select_all_btn = ctk.CTkButton(bottom_frame, text="Select All", width=100, command=self.select_all)
        select_all_btn.pack(side="left", padx=5)
        
        deselect_all_btn = ctk.CTkButton(bottom_frame, text="Deselect All", width=100, command=self.deselect_all)
        deselect_all_btn.pack(side="left", padx=5)
        
        self.push_btn = ctk.CTkButton(bottom_frame, font=ctk.CTkFont(weight="bold"), command=self.push_selected)
        
        if is_scraping_active:
            self.push_btn.configure(text="Scraping Active (Push Disabled)", state="disabled", fg_color="gray")
        else:
            self.on_tab_change() 
            
        self.push_btn.pack(side="right", padx=5)

        self._monitor_engine_state()

    def _monitor_engine_state(self):
        try:
            is_scraping_active = self._get_scraping_status()
                
            if not is_scraping_active:
                if self.push_btn.cget("state") == "disabled":
                    self.on_tab_change() 
                
                for ref in self.row_data_refs:
                    ref['cb_widget'].configure(state="normal")
                    ref['tag_widget'].configure(text_color="white")
                    ref['stock_widget'].configure(state="normal", fg_color="#34495E")
                    ref['price_widget'].configure(state="normal")
            else:
                self.after(1000, self._monitor_engine_state)
        except Exception:
            pass

    def on_tab_change(self):
        current_sup = self.tabview.get()
        if self.push_btn.cget("state") != "disabled":
            if current_sup == "No Updates":
                self.push_btn.configure(text="No Updates to Push", state="disabled", fg_color="gray")
            else:
                self.push_btn.configure(text=f"Push Selected to Sheet ({current_sup})", state="normal", fg_color="#2FA572", hover_color="#1F7A50")

    def select_all(self):
        current_sup = self.tabview.get()
        for ref in self.row_data_refs:
            if ref['supplier'] == current_sup: 
                ref['check_var'].set(True)

    def deselect_all(self):
        current_sup = self.tabview.get()
        for ref in self.row_data_refs:
            if ref['supplier'] == current_sup: 
                ref['check_var'].set(False)

    def clear_all_data(self):
        if messagebox.askyesno("Confirm Clear", "Are you sure you want to delete ALL pending and verified updates?\n\nThis cannot be undone."):
            self.engine.pending_updates.clear()
            if hasattr(self.engine.validator, 'pending_items'):
                self.engine.validator.pending_items.clear()
            if hasattr(self.engine.validator, 'pass1_data'):
                self.engine.validator.pass1_data.clear()
            
            self.engine.save_recovery_data()
            self.log("User manually cleared all items from the Review Window.", "info")
            self.destroy()

    def on_window_close(self):
        for ref in self.row_data_refs:
            row_num = ref['row_num']
            new_p = ref['price_var'].get()
            new_s = ref['stock_var'].get()

            if ref['is_verified']:
                for item in self.engine.pending_updates:
                    if item['row_num'] == row_num:
                        item['price'] = new_p
                        item['stock'] = new_s
                        break
            else:
                for item in self.engine.validator.pending_items:
                    if item.get('row_num') == row_num:
                        item['pass1_price'] = new_p
                        item['pass1_stock'] = new_s
                        break

        self.engine.save_recovery_data()
        self.destroy()

    def push_selected(self):
        current_sup = self.tabview.get()
        if current_sup == "No Updates": 
            return

        master_payload = []
        pushed_refs = []
        
        for ref in self.row_data_refs:
            if ref['supplier'] == current_sup and ref['check_var'].get():
                row_num = ref['row_num']
                url = ref['url']
                
                stock_val = ref['stock_var'].get()
                price_val = ref['price_var'].get().strip()
                ai_note = ref['ai_note_var'].get().strip()

                # Process AI Note during push as a fallback
                if ai_note:
                    if hasattr(self.engine.ai_memory, 'add_user_rule'):
                        self.engine.ai_memory.add_user_rule(url, ai_note)
                        self.log(f"Saved custom AI Directive for URL: {url}", "warning")

                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                master_payload.append({"range": f"A{row_num}", "values": [[f"Updated: {timestamp}"]]})
                
                if stock_val != "Blank":
                    master_payload.append({"range": f"C{row_num}", "values": [[stock_val]]})
                
                if price_val and price_val != "$":
                    master_payload.append({"range": f"P{row_num}", "values": [[price_val]]})
                
                pushed_refs.append(ref)

        if not pushed_refs:
            messagebox.showinfo("Info", f"No verified updates selected for {current_sup}.")
            return

        try:
            self.sheets_manager.batch_update(master_payload)
            self.log(f"Successfully pushed {len(pushed_refs)} edited updates for [{current_sup}] to Google Sheets.", "info")
            
            pushed_rows = [r['row_num'] for r in pushed_refs]
            self.engine.pending_updates = [i for i in self.engine.pending_updates if i['row_num'] not in pushed_rows]
            
            if hasattr(self.engine.validator, 'pending_items'):
                self.engine.validator.pending_items = [i for i in self.engine.validator.pending_items if i.get('row_num') not in pushed_rows]
            
            self.engine.save_recovery_data()
            
            for ref in pushed_refs:
                ref['row_frame'].destroy()
                self.row_data_refs.remove(ref)
            
            messagebox.showinfo("Success", f"{len(pushed_refs)} updates for [{current_sup}] pushed to Google Sheets successfully.")
            
            if not self.row_data_refs:
                self.destroy()
            
        except Exception as e:
            messagebox.showerror("Update Error", f"Failed to push updates: {str(e)}")