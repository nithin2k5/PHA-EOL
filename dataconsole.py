import tkinter as tk
from tkinter import ttk, messagebox
from tkcalendar import DateEntry
import mysql.connector
import customtkinter as ctk

import db
import ui
from datetime import datetime, timedelta
import csv
import os

# The cameras' pictures of each test. Without OpenCV and Pillow the page
# still works; the camera panels then show the verdicts alone.
try:
    import cv2
    from PIL import Image, ImageTk
    from vision_engine import captures
except ImportError:
    captures = None


# Results columns: heading, width, whether it takes up spare width. The
# readings are short numbers, so they stay narrow and the spec data - the
# one long value - gets whatever the window has left over.
COLUMNS = (
    ("NO", 55, False),
    ("LOT NUMBER", 135, False),
    ("PART NUMBER", 140, False),
    ("L1", 80, False),
    ("L2", 80, False),
    ("L3", 80, False),
    ("L4", 80, False),
    ("P1", 80, False),
    ("P2", 80, False),
    ("P3", 80, False),
    ("P4", 80, False),
    ("CAM1", 80, False),
    ("CAM2", 80, False),
    ("RESULT", 90, False),
    ("SCAN RESULT", 125, False),
    ("EMPLOYEE CODE", 165, False),
    ("SPEC DATA", 240, True),
    ("CREATED DATE", 185, False),
)

# Where the result sits in a fetched row. The query selects the ID first, in
# the place the NO column takes, so a row lines up with COLUMNS.
RESULT_AT = [heading for heading, _, _ in COLUMNS].index("RESULT")
CAM_AT = {"CAM1": [heading for heading, _, _ in COLUMNS].index("CAM1"),
          "CAM2": [heading for heading, _, _ in COLUMNS].index("CAM2")}

# Verdict colours, as on the test console's camera tiles
VERDICT_COLOURS = {"PASS": ui.SUCCESS, "NG": ui.DANGER, "ERROR": ui.WARNING_HOVER}

# Shown in a cell with no value, rather than Python's "None".
BLANK = "—"


class DataConsole:
    def __init__(self, root):
        # Accept parent window instead of creating new one
        self.root = root
        ui.apply(root)
        self.root.title("EOL (END OF LINE) TESTER")

        # Database configuration
        self.db_config = db.get_config()
        self.columns = [heading for heading, _, _ in COLUMNS]
        # The rows on show, as raw values, for the export.
        self.export_rows = []
        # Each row's camera verdicts and pictures, by table row id
        self.row_cameras = {}

        ui.page_header(self.root, "Work Data")
        # The footer is packed before the body so it keeps the foot of the window
        ui.footer_bar(self.root)

        body = tk.Frame(self.root, bg=ui.APP_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=ui.PAD_LARGE, pady=ui.PAD_LARGE)

        self.create_summary(body)
        self.create_filter_panel(body)
        self.create_table(body)

        self.load_part_numbers()
        # Open on this month's results rather than an empty table.
        self.root.after(100, lambda: self.search_records(quiet=True))

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def create_summary(self, parent):
        """What is on show on the left, its totals in tiles on the right."""
        row = tk.Frame(parent, bg=ui.APP_BG)
        row.pack(fill=tk.X)

        heading = tk.Frame(row, bg=ui.APP_BG)
        heading.pack(side=tk.LEFT, anchor='w')
        tk.Label(heading, text="TEST HISTORY", bg=ui.APP_BG, fg=ui.ACCENT,
                 font=(ui.FONT_FAMILY, 20, 'bold')).pack(anchor='w')
        # What the table is showing, so a printout or photo of it says so.
        self.range_label = tk.Label(heading, text="", bg=ui.APP_BG, fg=ui.TEXT_MUTED,
                                    font=(ui.FONT_FAMILY, 11, 'bold'))
        self.range_label.pack(anchor='w')

        self.stats = {}
        # Packed from the right, so they read PARTS, PASS, NG, NG %
        for key, title, colour in reversed((
                ('total', "PARTS", ui.ACCENT),
                ('pass', "PASS", ui.SUCCESS),
                ('ng', "NG", ui.DANGER),
                ('rate', "NG %", ui.WARNING))):
            tile = ui.ctk_card(row, width=120, height=72)
            tile.pack(side=tk.RIGHT, padx=(ui.PAD, 0))
            tile.pack_propagate(False)

            value = ctk.CTkLabel(tile, text=BLANK, text_color=colour,
                                 font=(ui.FONT_FAMILY, 22, 'bold'))
            value.pack(pady=(ui.PAD, 0))
            ctk.CTkLabel(tile, text=title, text_color=ui.TEXT_MUTED,
                         font=(ui.FONT_FAMILY, 11, 'bold')).pack()
            self.stats[key] = value

    def create_filter_panel(self, parent):
        """The filters and quick ranges, the actions, then both cameras' pictures."""
        card = ui.ctk_card(parent)
        card.pack(fill=tk.X, pady=(ui.PAD_LARGE, 0))

        panel = tk.Frame(card, bg=ui.SURFACE)
        panel.pack(fill=tk.X, padx=ui.PAD_LARGE, pady=ui.PAD_LARGE)

        self.create_filters(panel)

        tk.Frame(panel, bg=ui.BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y,
                                                    padx=ui.PAD_LARGE)

        actions = tk.Frame(panel, bg=ui.SURFACE)
        actions.pack(side=tk.LEFT, anchor='n')
        self.search_button = ui.ctk_button(actions, text="Search", icon='clipboard',
                                           kind='primary', width=170, height=40,
                                           command=self.search_records)
        self.search_button.pack(pady=(ui.PAD_LARGE, ui.PAD))
        self.export_button = ui.ctk_button(actions, text="Export CSV", icon='download',
                                           kind='success', width=170, height=40,
                                           command=self.export_to_csv)
        self.export_button.pack()

        tk.Frame(panel, bg=ui.BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y,
                                                    padx=ui.PAD_LARGE)

        self.create_camera_panels(panel)

    def create_filters(self, parent):
        """Part number, result and dates over the quick date ranges."""
        box = tk.Frame(parent, bg=ui.SURFACE)
        box.pack(side=tk.LEFT, anchor='n')

        def field(column, text, widget):
            tk.Label(box, text=text, bg=ui.SURFACE, fg=ui.TEXT_MUTED,
                     font=(ui.FONT_FAMILY, 10, 'bold')).grid(
                         row=0, column=column, sticky='w',
                         padx=(0 if column == 0 else ui.PAD_LARGE, 0))
            widget.grid(row=1, column=column, sticky='w',
                        padx=(0 if column == 0 else ui.PAD_LARGE, 0), pady=(2, 0))

        self.part_combobox = ttk.Combobox(box, state="readonly", width=16)
        field(0, "PART NUMBER", self.part_combobox)

        self.result_combobox = ttk.Combobox(box, state="readonly", width=8,
                                            values=["ALL", "PASS", "NG"])
        self.result_combobox.set("ALL")
        field(1, "RESULT", self.result_combobox)

        # Start at the beginning of the current month, end today.
        current_date = datetime.now()
        self.start_date_entry = self.date_entry(box, current_date)
        self.start_date_entry.set_date(current_date.replace(day=1))
        field(2, "START DATE", self.start_date_entry)

        self.end_date_entry = self.date_entry(box, current_date)
        self.end_date_entry.set_date(current_date)
        field(3, "END DATE", self.end_date_entry)

        quick = tk.Frame(box, bg=ui.SURFACE)
        quick.grid(row=2, column=0, columnspan=4, sticky='w', pady=(ui.PAD_LARGE * 2, 0))
        tk.Label(quick, text="QUICK", bg=ui.SURFACE, fg=ui.TEXT_MUTED,
                 font=(ui.FONT_FAMILY, 10, 'bold')).pack(side=tk.LEFT, padx=(0, ui.PAD))
        for text, days in (("TODAY", 0), ("7 DAYS", 6), ("30 DAYS", 29), ("THIS MONTH", None)):
            tk.Button(quick, text=text, bg=ui.ACCENT_SOFT, fg=ui.ACCENT,
                      activebackground=ui.ACCENT_FILL, relief='flat', bd=0,
                      cursor='hand2', font=(ui.FONT_FAMILY, 10, 'bold'),
                      padx=10, pady=3,
                      command=lambda d=days: self.quick_range(d)).pack(
                          side=tk.LEFT, padx=(0, ui.PAD))

    def quick_range(self, days):
        """Search the last `days` days besides today, or this month when None."""
        today = datetime.now()
        start = today.replace(day=1) if days is None else today - timedelta(days=days)
        self.start_date_entry.set_date(start)
        self.end_date_entry.set_date(today)
        self.search_records()

    def date_entry(self, parent, max_date):
        return DateEntry(parent,
                         width=11,
                         date_pattern='dd-mm-yyyy',
                         background=ui.ACCENT_FILL,
                         foreground=ui.TEXT_ON_ACCENT,
                         borderwidth=0,
                         font=ui.FONT_BODY,
                         showweeknumbers=False,
                         showothermonthdays=True,
                         firstweekday='sunday',
                         maxdate=max_date,
                         selectmode='day',
                         cursor='hand2')

    def create_table(self, parent):
        table_card = ui.ctk_card(parent)
        table_card.pack(fill=tk.BOTH, expand=True, pady=(ui.PAD_LARGE, 0))

        table_frame = tk.Frame(table_card, bg=ui.SURFACE)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=ui.PAD_LARGE,
                         pady=ui.PAD_LARGE)
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        self.result_table = ttk.Treeview(table_frame, columns=self.columns,
                                         show="headings")
        scroll_y = ttk.Scrollbar(table_frame, orient=tk.VERTICAL,
                                 command=self.result_table.yview)
        scroll_x = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL,
                                 command=self.result_table.xview)
        self.result_table.configure(yscrollcommand=scroll_y.set,
                                    xscrollcommand=scroll_x.set)

        self.result_table.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")

        for heading, width, stretch in COLUMNS:
            self.result_table.heading(heading, text=heading)
            self.result_table.column(heading, width=width, minwidth=width,
                                     anchor="w" if stretch else "center",
                                     stretch=stretch)

        # Bands of five in light yellow, as on the Test console's results;
        # a failed part also reads red, so NG rows stand out in a long list.
        self.result_table.tag_configure('band', background=ui.ROW_BAND)
        self.result_table.tag_configure('plain', background=ui.ROW_PLAIN)
        self.result_table.tag_configure('ng', foreground=ui.DANGER)

        self.result_table.bind("<<TreeviewSelect>>", self.show_cameras)

        # Laid over the table while it has nothing to show.
        self.empty_label = tk.Label(table_frame, bg=ui.SURFACE, fg=ui.TEXT_MUTED,
                                    font=ui.FONT_SECTION,
                                    text="Choose the filters above and press Search.")
        self.empty_label.place(relx=0.5, rely=0.5, anchor="center")

    def create_camera_panels(self, parent):
        """CAMERA 1 and CAMERA 2: what each camera saw of the selected test."""
        self.camera_panels = {}
        for key, title in (("CAM1", "CAMERA 1"), ("CAM2", "CAMERA 2")):
            box = tk.Frame(parent, bg=ui.SURFACE)
            box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                     padx=(0 if key == "CAM1" else ui.PAD_LARGE, 0))

            caption = tk.Frame(box, bg=ui.SURFACE)
            caption.pack(fill=tk.X)
            tk.Label(caption, text=title, bg=ui.SURFACE, fg=ui.TEXT_MUTED,
                     font=(ui.FONT_FAMILY, 10, 'bold')).pack(side=tk.LEFT)
            # A badge in the verdict's colour, as on the test console's tiles
            verdict = tk.Label(caption, text="", bg=ui.SURFACE, fg=ui.TEXT_ON_DARK,
                               font=(ui.FONT_FAMILY, 10, 'bold'), padx=8)
            verdict.pack(side=tk.RIGHT)

            # A fixed height, so a picture never pushes the table off the page
            frame = tk.Frame(box, bg=ui.SUBTLE, height=170,
                             highlightbackground=ui.BORDER, highlightthickness=1)
            frame.pack(fill=tk.X, pady=(2, 0))
            frame.pack_propagate(False)
            picture = tk.Label(frame, bg=ui.SUBTLE, fg=ui.TEXT_MUTED,
                               font=ui.FONT_BODY, text="Select a row")
            picture.pack(fill=tk.BOTH, expand=True)
            panel = {"verdict": verdict, "picture": picture, "image": None, "photo": None,
                     "title": title}
            # Fit the picture again whenever the panel changes size
            picture.bind("<Configure>", lambda e, p=panel: self.fit_picture(p))
            # The panel is small; click it to look at the picture properly
            picture.bind("<Button-1>", lambda e, p=panel: self.open_picture(p))
            self.camera_panels[key] = panel

    def show_cameras(self, event=None):
        """Fill both camera panels from the selected result row."""
        selected = self.result_table.selection()
        cameras = self.row_cameras.get(selected[0]) if selected else None
        for key, panel in self.camera_panels.items():
            verdict, picture = cameras[key] if cameras else ("", None)
            panel["verdict"].configure(
                text=verdict or "",
                bg=VERDICT_COLOURS.get(str(verdict).upper(), ui.NAVY) if verdict else ui.SURFACE)
            panel["image"] = captures.load(picture) if captures and picture else None
            if panel["image"] is None:
                panel["photo"] = None
                if not cameras:
                    note = "Select a row"
                elif picture and captures:
                    note = "This picture is no longer kept"
                else:
                    note = "No picture was taken for this test"
                panel["picture"].configure(image="", text=note)
            panel["picture"].configure(cursor="hand2" if panel["image"] is not None else "")
            self.fit_picture(panel)

    @staticmethod
    def fit_picture(panel):
        """Show the panel's picture as large as fits, keeping its shape."""
        img = panel["image"]
        label = panel["picture"]
        if img is None:
            return
        w, h = label.winfo_width() - 4, label.winfo_height() - 4
        if w < 10 or h < 10:
            return
        scale = min(w / img.shape[1], h / img.shape[0])
        size = (max(1, int(img.shape[1] * scale)), max(1, int(img.shape[0] * scale)))
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        panel["photo"] = ImageTk.PhotoImage(
            Image.fromarray(rgb).resize(size, Image.Resampling.BILINEAR))
        label.configure(image=panel["photo"], text="")

    def open_picture(self, panel):
        """Show a camera panel's picture in a window of its own.

        It opens fitted to the window. The wheel zooms in on the point under
        the pointer, dragging moves the picture, a double-click fits it
        again, and Escape closes the window.
        """
        img = panel["image"]
        if img is None:
            return
        verdict = panel["verdict"].cget("text")
        win = tk.Toplevel(self.root, bg="black")
        win.title(f"{panel['title']} - {verdict}" if verdict else panel["title"])
        win.transient(self.root)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        ww, wh = int(sw * 0.8), int(sh * 0.8)
        win.geometry(f"{ww}x{wh}+{(sw - ww) // 2}+{(sh - wh) // 3}")
        canvas = tk.Canvas(win, bg="black", highlightthickness=0, cursor="fleur")
        canvas.pack(fill=tk.BOTH, expand=True)
        tk.Label(win, text="Wheel: zoom    Drag: move    Double-click: fit    Esc: close",
                 bg=ui.NAVY, fg=ui.TEXT_ON_DARK, font=ui.FONT_BODY).pack(fill=tk.X)

        rgb = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        # Scale is screen pixels per picture pixel; (ox, oy) is where the
        # picture's top-left corner sits on the canvas
        view = {"scale": None, "ox": 0.0, "oy": 0.0, "drag": None, "photo": None}

        def fit():
            cw, ch = canvas.winfo_width(), canvas.winfo_height()
            if cw < 10 or ch < 10:
                return
            view["scale"] = min(cw / rgb.width, ch / rgb.height)
            view["ox"] = (cw - rgb.width * view["scale"]) / 2
            view["oy"] = (ch - rgb.height * view["scale"]) / 2
            draw()

        def draw():
            s = view["scale"]
            cw, ch = canvas.winfo_width(), canvas.winfo_height()
            canvas.delete("all")
            # Scale only the part on screen, so a close zoom stays quick
            x0 = max(0, int(-view["ox"] / s))
            y0 = max(0, int(-view["oy"] / s))
            x1 = min(rgb.width, int((cw - view["ox"]) / s) + 2)
            y1 = min(rgb.height, int((ch - view["oy"]) / s) + 2)
            if x1 <= x0 or y1 <= y0:
                return
            size = (max(1, round((x1 - x0) * s)), max(1, round((y1 - y0) * s)))
            # Close in, show the real pixels rather than a blur
            resample = Image.Resampling.NEAREST if s >= 3 else Image.Resampling.BILINEAR
            view["photo"] = ImageTk.PhotoImage(rgb.crop((x0, y0, x1, y1)).resize(size, resample))
            canvas.create_image(view["ox"] + x0 * s, view["oy"] + y0 * s,
                                image=view["photo"], anchor="nw")

        def zoom(event):
            if view["scale"] is None:
                return
            cw, ch = canvas.winfo_width(), canvas.winfo_height()
            least = min(cw / rgb.width, ch / rgb.height)
            factor = 1.25 if event.delta > 0 else 1 / 1.25
            new = min(max(view["scale"] * factor, least), least * 16)
            # Keep the picture point under the pointer where it is
            ix = (event.x - view["ox"]) / view["scale"]
            iy = (event.y - view["oy"]) / view["scale"]
            view["scale"] = new
            view["ox"], view["oy"] = event.x - ix * new, event.y - iy * new
            if new <= least * 1.0001:
                fit()
            else:
                draw()

        def press(event):
            view["drag"] = (event.x, event.y, view["ox"], view["oy"])

        def move(event):
            if view["drag"] and view["scale"] is not None:
                x, y, ox, oy = view["drag"]
                view["ox"], view["oy"] = ox + event.x - x, oy + event.y - y
                draw()

        canvas.bind("<Configure>", lambda e: fit())
        canvas.bind("<MouseWheel>", zoom)
        canvas.bind("<ButtonPress-1>", press)
        canvas.bind("<B1-Motion>", move)
        canvas.bind("<ButtonRelease-1>", lambda e: view.update(drag=None))
        canvas.bind("<Double-Button-1>", lambda e: fit())
        win.bind("<Escape>", lambda e: win.destroy())
        win.focus_set()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def load_part_numbers(self):
        """Load part numbers from TBL_MODEL_MASTER into combobox"""
        try:
            conn = mysql.connector.connect(**self.db_config)
            cursor = conn.cursor()

            cursor.execute("SELECT MM_PART_NUMBER FROM TBL_MODEL_MASTER ORDER BY MM_PART_NUMBER")
            part_numbers = [row[0] for row in cursor.fetchall()]

            # Add "ALL" option at the beginning
            part_numbers.insert(0, "ALL")
            self.part_combobox['values'] = part_numbers
            self.part_combobox.set("ALL")

            cursor.close()
            conn.close()

        except mysql.connector.Error as err:
            messagebox.showerror("Database Error", f"Failed to load part numbers: {err}")

    @staticmethod
    def display(value):
        if value is None or value == "":
            return BLANK
        if isinstance(value, datetime):
            return value.strftime('%d-%m-%Y  %H:%M:%S')
        return value

    def search_records(self, quiet=False):
        """Search records based on filters.

        `quiet` is for the search made on opening the page: a database that
        cannot be reached is then left to the empty table to show, rather
        than greeting the operator with an error box.
        """
        self.result_table.delete(*self.result_table.get_children())
        self.export_rows = []
        self.row_cameras = {}

        start = self.start_date_entry.get_date()
        end = self.end_date_entry.get_date()
        part_number = self.part_combobox.get()
        result_filter = self.result_combobox.get()

        conn = None
        try:
            conn = mysql.connector.connect(**self.db_config)
            cursor = conn.cursor()

            # Build query with dynamic filters for TBL_TEST_RESULTS
            query = """
                SELECT
                    ID, LOT_NUMBER, PART_NUMBER, L1, L2, L3, L4, P1, P2, P3, P4,
                    CAM1, CAM2, RESULT, SCAN_RESULT, EMP_CODE, SPEC_DATA, CREATED_DATE,
                    CAM1_IMAGE, CAM2_IMAGE
                FROM TBL_TEST_RESULTS
                WHERE DATE(CREATED_DATE) BETWEEN %s AND %s
            """
            params = [start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')]

            # Add part number filter if not ALL
            if part_number and part_number != "ALL":
                query += " AND PART_NUMBER = %s"
                params.append(part_number)

            # Add result filter if not ALL
            if result_filter and result_filter != "ALL":
                query += " AND RESULT = %s"
                params.append(result_filter)

            # Newest first; tests saved in the same second by the order saved
            query += " ORDER BY CREATED_DATE DESC, ID DESC"

            cursor.execute(query, tuple(params))
            records = cursor.fetchall()
            cursor.close()

        except mysql.connector.Error as err:
            self.show_summary([])
            self.empty_label.config(text="Could not reach the database.")
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
            if not quiet:
                messagebox.showerror("Database Error", f"Search failed: {err}")
            return

        finally:
            if conn is not None and conn.is_connected():
                conn.close()

        for i, record in enumerate(records, 1):
            # The table's columns, then the two pictures' paths
            shown, pictures = record[:len(COLUMNS)], record[len(COLUMNS):]
            raw = [i] + list(shown)[1:]  # Add row number, skip ID
            self.export_rows.append(raw)
            tags = ['band' if ((i - 1) // 5) % 2 == 0 else 'plain']
            if str(record[RESULT_AT]).upper() == 'NG':
                tags.append('ng')
            row_id = self.result_table.insert('', 'end', values=[self.display(v) for v in raw],
                                              tags=tuple(tags))
            self.row_cameras[row_id] = {"CAM1": (record[CAM_AT["CAM1"]], pictures[0]),
                                        "CAM2": (record[CAM_AT["CAM2"]], pictures[1])}

        # Open on the newest test's pictures
        rows = self.result_table.get_children()
        if rows:
            self.result_table.selection_set(rows[0])
        self.show_cameras()

        self.range_label.configure(text="{}  ·  {}  ·  {} TO {}".format(
            "ALL PARTS" if part_number in ("", "ALL") else part_number,
            "ALL RESULTS" if result_filter in ("", "ALL") else result_filter,
            start.strftime('%d-%m-%Y'), end.strftime('%d-%m-%Y')))
        self.show_summary(records)

        if records:
            self.empty_label.place_forget()
        else:
            self.empty_label.config(text="No records match these filters.")
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")

    def show_summary(self, records):
        total = len(records)
        passed = sum(1 for r in records if str(r[RESULT_AT]).upper() == 'PASS')
        failed = sum(1 for r in records if str(r[RESULT_AT]).upper() == 'NG')
        self.stats['total'].configure(text=str(total))
        self.stats['pass'].configure(text=str(passed))
        self.stats['ng'].configure(text=str(failed))
        self.stats['rate'].configure(
            text="{:.1f}".format(100.0 * failed / total) if total else BLANK)

    def export_to_csv(self):
        """Export table data to CSV file"""
        try:
            # Check if there's data to export
            if not self.export_rows:
                messagebox.showwarning("No Data", "No data to export. Please search first.")
                return

            # Generate filename with timestamp
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"EOL_Data_Export_{timestamp}.csv"

            # Get desktop path
            desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
            filepath = os.path.join(desktop, filename)

            # Write to CSV
            with open(filepath, 'w', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow(self.columns)
                for row in self.export_rows:
                    writer.writerow(['' if value is None else value for value in row])

            messagebox.showinfo("Export Successful", f"Data exported to:\n{filepath}")

        except Exception as e:
            messagebox.showerror("Export Error", f"Failed to export data: {e}")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    root = tk.Tk()
    app = DataConsole(root)
    root.mainloop()
