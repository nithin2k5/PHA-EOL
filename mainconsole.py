import tkinter as tk
from tkinter import ttk, messagebox
import configparser
import csv
import os
import time
from datetime import datetime

import serial
from model_settings import WorkspaceApp  # Import ModelSettings
from test_console import EOLTesterGUI    # Import TestConsole
from comport_settings import ComPortSettings  # Add this import
from dataconsole import DataConsole  # Add this import
from adminconsole import AdminConsole  # Add this import
from vision_settings import VisionSettings
from helpconsole import HelpConsole, ContactConsole
from login_form import prompt_login
import config
import db
import ui
import icons

import customtkinter as ctk


class PageFrame(tk.Frame):
    """A panel a console can be built into as though it were its own window.

    Each console was written to own a top level window: it names itself,
    maximises itself and registers a WM_DELETE_WINDOW handler. Hosted in a
    panel those calls have nowhere to go, so they are answered here - the
    title goes to the shell's window, the close handler is kept for the
    shell to call when the page is replaced, and the rest are dropped.
    """

    def __init__(self, master, shell, **kwargs):
        super().__init__(master, **kwargs)
        self.shell = shell
        self.close_handler = None

    def title(self, text=None):
        if text is None:
            return self.shell.wm_title()
        self.shell.wm_title(text)

    def protocol(self, name, callback=None):
        if name == 'WM_DELETE_WINDOW' and callback is not None:
            self.close_handler = callback
        return ''

    # A panel already fills the side of the window it was handed, so there
    # is nothing here to maximise, raise above other windows or resize.
    def state(self, *args):
        return 'normal'

    def attributes(self, *args):
        return ''

    def geometry(self, *args):
        return ''

    def resizable(self, *args):
        return ''

    def destroy(self):
        # A console closing itself through root.destroy(): the shell cannot
        # be left holding a panel that is no longer on screen.
        shell = self.shell
        super().destroy()
        if shell is not None:
            shell.page_closed(self)


class NavButton(ctk.CTkButton):
    """One entry in the navy navigation rail: a rounded pill, icon over label.

    `command` runs on click; `set_active` marks this as the console
    currently on show, filling it sky blue like the Test console's lamps.
    """

    ICON_SIZE = 22
    FONT = (ui.FONT_FAMILY, 10, 'bold')
    INK = ui.TEXT_ON_DARK        # white on the navy rail
    DANGER_INK = '#FF8080'       # exit, light enough to read on navy
    HOVER = '#2E2E8C'
    DISABLED_INK = '#7F7FB0'

    def __init__(self, master, icon, label, command, danger=False):
        self.icon_name = icon
        self.danger = danger
        self.active = False

        super().__init__(
            master, text=label, command=command, height=64,
            corner_radius=ui.CORNER_RADIUS_SMALL, compound='top',
            font=self.FONT, fg_color='transparent', bg_color=ui.NAVY,
            hover_color=self.HOVER,
            text_color=self.DANGER_INK if danger else self.INK,
            text_color_disabled=self.DISABLED_INK,
        )
        self._apply_icon(self.DANGER_INK if danger else self.INK)

    def _apply_icon(self, color):
        self.configure(image=ui.icon_image(self.icon_name, color, self.ICON_SIZE))

    def set_enabled(self, enabled):
        """Stand in for the `state` option a real button would take."""
        self.configure(state='normal' if enabled else 'disabled')
        if not enabled:
            self.set_active(False)
        self._apply_icon(self.DISABLED_INK if not enabled else
                         (ui.TEXT_ON_ACCENT if self.active else
                          (self.DANGER_INK if self.danger else self.INK)))

    def set_active(self, active):
        """Mark this as the console currently on show."""
        self.active = bool(active)
        if self.active:
            self.configure(fg_color=ui.SKY, hover_color=ui.ACCENT_HOVER,
                           text_color=ui.TEXT_ON_ACCENT)
            self._apply_icon(ui.TEXT_ON_ACCENT)
        else:
            text_color = self.DANGER_INK if self.danger else self.INK
            self.configure(fg_color='transparent', hover_color=self.HOVER,
                           text_color=text_color)
            self._apply_icon(text_color)


class MainConsole(tk.Tk):
    # Records older than this are written out to the archive folders.
    ARCHIVE_AFTER_DAYS = 120
    # Warn when the last test on this machine is older than this.
    STALE_TEST_WARNING_DAYS = 2
    # Share of the window width given to the side navigation.
    NAV_SHARE = 0.05
    # The tab on the navigation's edge that puts it away and brings it back.
    NAV_TAB_WIDTH = 22
    NAV_TAB_HEIGHT = 64
    # How long the navigation takes to slide away or back.
    NAV_SLIDE_MS = 180
    # How long the navigation stays out after a page is opened from it.
    NAV_HIDE_DELAY_MS = 3000

    def __init__(self):
        super().__init__()
        ui.apply(self)
        self.title("EOL Tester")
        self.connect_database()
        self.state('zoomed')  # Start maximized

        # A clock behind the last recorded test would misdate new results.
        if not self.check_last_test_date():
            self.destroy()
            return

        self.setup_ui()
        
    def connect_database(self):
        """Set up the database, saying plainly when it can't be reached.

        Returns True once it is ready. Without it nothing can be tested or
        saved, so the operator can retry - after starting MySQL, say - or
        open the app anyway to reach the pages that don't need it.
        """
        while True:
            try:
                db.init_database(raise_on_error=True)
                return True
            except Exception as e:
                code = getattr(e, 'errno', None)
                if code in (2003, 2005):
                    reason = ("The MySQL server could not be reached. Check that MySQL "
                              "is running on this computer.")
                elif code == 1045:
                    reason = ("MySQL refused the user name or password. Check the "
                              "USER and PASSWORD in db.py.")
                else:
                    reason = "MySQL reported a problem."
                retry = messagebox.askretrycancel(
                    "Database Connection Failed",
                    f"{reason}\n\n"
                    f"Server: {db.HOST}:{db.PORT}    User: {db.USER}    "
                    f"Database: {db.DATABASE}\n"
                    f"Error: {e}\n\n"
                    "Retry to try again, or Cancel to open without the database - "
                    "testing and saving results will not work until it connects.",
                    parent=self)
                if not retry:
                    return False

    def setup_ui(self):
        # Set the main window to full screen
        self.state('zoomed')
        
        # Get screen dimensions
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        self.geometry(f"{screen_width}x{screen_height}+0+0")
        
        # No bar of the shell's own: every page, and the empty panel below,
        # carries the pink title bar with the logo and machine ID.

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        # Pages are built in here, filling the window; the navigation lies
        # over them. Propagation is off so that a console laid out wider
        # than the window cannot stretch it.
        self.body = body
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.page_host = ttk.Frame(body, width=1, height=1)
        self.page_host.grid(row=0, column=0, sticky="nsew")
        self.page_host.pack_propagate(False)

        self.page = None
        self.page_app = None
        self.page_closed_hook = None
        self.placeholder = tk.Frame(self.page_host, bg=ui.APP_BG)
        ui.title_bar(self.placeholder, "EOL (END OF LINE) TESTER")
        tk.Label(self.placeholder, bg=ui.APP_BG, fg=ui.TEXT_MUTED, font=ui.FONT_SECTION,
                 text="Choose a console from the navigation on the left.").pack(
                     fill="both", expand=True)
        self.show_placeholder()

        self.build_nav_drawer(body)

        # Create menu. A tk.Menu is drawn by the window manager rather than
        # by the theme, so it keeps a system-light bar above a dark window
        # unless it is coloured by hand.
        menu_colours = dict(bg=ui.SURFACE, fg=ui.TEXT,
                            activebackground=ui.ACCENT_SOFT,
                            activeforeground=ui.ACCENT,
                            borderwidth=0)
        menubar = tk.Menu(self, **menu_colours)
        self.config(menu=menubar)

        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0, **menu_colours)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="Help", command=self.user_manual_click)
        help_menu.add_command(label="Contact", command=self.support_click)

        # Closing the window has to take the open page with it, or the test
        # console's serial threads and COM ports are left behind.
        self.protocol("WM_DELETE_WINDOW", self.exit_click)

        self.load_settings()

    def build_side_nav(self, parent):
        """The left-hand navigation rail.

        Each entry is one row of a single grid column, so they line up on
        both edges and stay the same size whatever their label says. A
        rounded logo mark sits above them as the rail's visual anchor.
        """
        nav = tk.Frame(parent, bg=ui.NAVY, width=1)
        # The column's width comes from the weights on the body, so the frame
        # must not ask for a width of its own: grid honours the widest child,
        # which would pull the column back open past its share.
        nav.grid_propagate(False)
        nav.grid_columnconfigure(0, weight=1)

        logo = ctk.CTkFrame(nav, width=44, height=44, corner_radius=12,
                            fg_color=ui.SKY, bg_color=ui.NAVY)
        logo.grid(row=0, column=0, pady=(ui.PAD_LARGE, ui.PAD))
        logo.grid_propagate(False)
        ctk.CTkLabel(logo, text='', fg_color=ui.SKY,
                    image=ui.icon_image('gear', ui.TEXT_ON_ACCENT, 24)).pack(
                        expand=True)

        entries = (
            ('btn_com_settings', 'swap', "COM\nPorts", self.com_port_settings_click),
            ('btn_settings', 'gear', "Settings", self.settings_click),
            ('btn_vision', 'camera', "Vision", self.vision_click),
            ('btn_test', 'play', "Test", self.test_click),
            ('btn_work_data', 'bars', "Work\nData", self.work_data_click),
            ('btn_admin', 'shield', "Admin", self.admin_click),
            ('btn_help', 'question', "Help", self.user_manual_click),
            ('btn_contact', 'mail', "Contact", self.support_click),
        )

        # The entries share the rail's height equally, so it is filled top to
        # bottom rather than bunched at the top with a gap above Exit.
        self.nav_buttons = []
        for row, (attribute, icon, label, command) in enumerate(entries, start=1):
            nav.grid_rowconfigure(row, weight=1, uniform='nav')
            button = self.nav_button(nav, icon, label, command)
            button.grid(row=row, column=0, sticky="nsew", padx=6, pady=(6, 0))
            setattr(self, attribute, button)

        # Exit sits apart at the foot of the rail, below a rule, so it is
        # never clicked by someone reaching for the console above it.
        rule = len(entries) + 1
        tk.Frame(nav, bg=NavButton.DISABLED_INK, height=1).grid(
            row=rule, column=0, sticky="ew", padx=ui.PAD_LARGE, pady=(ui.PAD_LARGE, 0))

        nav.grid_rowconfigure(rule + 1, weight=1, uniform='nav')
        self.btn_exit = self.nav_button(nav, 'power', "Exit", self.exit_click,
                                        danger=True)
        self.btn_exit.grid(row=rule + 1, column=0, sticky="nsew", padx=6, pady=6)

        return nav

    def build_nav_drawer(self, parent):
        """The navigation and its tab, in a see-through window of their own over the page's left edge.

        Tk cannot make part of a window see-through, only a whole window,
        so the rail lives in a borderless window kept over this one. Its
        background is a colour Windows leaves out entirely, so only the
        rail and its tab show, and clicks beside them reach the page.
        """
        self.nav_width = max(60, round(self.winfo_screenwidth() * self.NAV_SHARE))
        self.nav_window = self.build_nav_window()
        self.nav = self.build_side_nav(self.nav_window)
        self.nav.configure(width=self.nav_width)

        # A navy tab on the navigation's right edge, halfway down, carrying
        # the triangle. It travels with the navigation and is left at the
        # window's edge when the navigation is away, to bring it back. It is
        # placed by a plain frame around it, because CTk scales a widget's
        # own place() coordinates for the display and it would then sit
        # adrift of the navigation's edge.
        self.nav_tab_holder = tk.Frame(self.nav_window, bg=ui.NAVY, exact_colors=True)
        self.nav_tab = ctk.CTkButton(
            self.nav_tab_holder, text='', command=self.toggle_nav,
            width=self.NAV_TAB_WIDTH, height=self.NAV_TAB_HEIGHT,
            corner_radius=0, border_width=0, fg_color=ui.NAVY,
            hover_color=NavButton.HOVER, cursor='hand2',
            image=ui.icon_image('triangle_left', ui.TEXT_ON_DARK, 16))
        self.nav_tab.pack()

        self.nav_open = True
        self.nav.place(x=0, y=0, relheight=1, width=self.nav_width)
        self.nav_tab_holder.place(x=self.nav_width, rely=0.5, anchor='w')

        self.nav_slide = None
        self.nav_hide_job = None
        self.nav_body = parent
        # Kept over the page as this window moves, resizes or is restored
        parent.bind('<Configure>', lambda e: self.place_nav(self.nav_open), add='+')
        self.bind('<Map>', lambda e: self.place_nav(self.nav_open), add='+')
        self.after_idle(lambda: self.place_nav(self.nav_open))

    # How much of the page shows through the navigation, 1.0 being solid
    NAV_ALPHA = 0.85
    # Painted where the navigation's window should not be at all
    NAV_CLEAR = '#010203'

    def build_nav_window(self):
        """A borderless window over this one, that never takes the focus.

        Tk keeps the window drawn see-through; who owns it and how it is
        activated are set on it by hand in own_nav_window(), as Tk does not
        give a borderless window an owner.
        """
        window = tk.Toplevel(self, bg=self.NAV_CLEAR, exact_colors=True)
        window.overrideredirect(True)
        try:
            window.attributes('-transparentcolor', self.NAV_CLEAR)
            window.attributes('-alpha', self.NAV_ALPHA)
        except tk.TclError as e:
            print(f"Navigation drawn solid: {e}")
        window.bind('<Map>', lambda e: self.own_nav_window(), add='+')
        self.bind('<Map>', lambda e: self.own_nav_window(), add='+')
        self.after_idle(self.own_nav_window)

        # Tk moves its focus into the rail when it is clicked, though
        # Windows never activates it; the focus goes straight back to the
        # console, so a scan after using the rail still lands in its box
        self.nav_return_focus = None
        self.bind_all('<FocusIn>', self.keep_focus_off_nav, add='+')
        return window

    def keep_focus_off_nav(self, event):
        """Remember where the focus is in the console, and send it back there from the rail."""
        widget = event.widget
        if not isinstance(widget, tk.Misc):
            return
        try:
            on_nav = widget.winfo_toplevel() is self.nav_window
        except tk.TclError:
            return
        if not on_nav:
            self.nav_return_focus = widget
            return
        back = self.nav_return_focus
        if back is not None and back.winfo_exists():
            self.after_idle(back.focus_set)

    def own_nav_window(self):
        """Make this window the navigation window's owner, and stop it ever being activated.

        Owned, it stays above this window, goes when it is minimised and
        sits beneath any dialog opened later. Never activated, a click on
        the rail leaves the focus in the console's entry boxes, where the
        barcode scanner types. Windows only; elsewhere the rail is an
        ordinary window.
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            nav = user32.GetParent(self.nav_window.winfo_id())
            owner = user32.GetParent(self.winfo_id())
            if not nav or not owner:
                return
            GWLP_HWNDPARENT, GWL_EXSTYLE = -8, -20
            WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
            user32.SetWindowLongPtrW(nav, GWLP_HWNDPARENT, owner)
            style = user32.GetWindowLongW(nav, GWL_EXSTYLE)
            user32.SetWindowLongW(nav, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
            # Brought above this window now, without being activated
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
            user32.SetWindowPos(nav, 0, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        except Exception as e:
            print(f"Navigation window not tied to the console: {e}")

    def nav_frame_box(self):
        """Where the navigation's window goes when out: over the page's left edge."""
        body = self.nav_body
        width = self.nav_width + self.nav_tab_holder.winfo_reqwidth()
        return body.winfo_rootx(), body.winfo_rooty(), width, body.winfo_height()

    def place_nav(self, shown, offset=None):
        """Put the navigation's window where it rests, out or away.

        Away, the rail sits off the window's left edge and only its tab
        shows. `offset` places it part way, for the slide.
        """
        if self.nav_slide is not None and offset is None:
            return      # the slide places it
        x, y, width, height = self.nav_frame_box()
        if height <= 1:
            return
        if offset is None:
            offset = 0 if shown else -self.nav_width
        self.nav_window.geometry(f"{width}x{height}+{x + offset}+{y}")

    def toggle_nav(self):
        """Put the navigation away, or bring it back, with the tab on its edge."""
        self.set_nav(not self.nav_open)

    def set_nav(self, shown):
        """Slide the navigation out or away, turning its tab's triangle to match."""
        # Whatever is asked for now overrides a put-away still waiting
        if self.nav_hide_job is not None:
            self.after_cancel(self.nav_hide_job)
            self.nav_hide_job = None
        if self.nav_slide is not None:
            self.end_nav_slide()
        if shown == self.nav_open:
            return
        self.nav_open = shown
        self.nav_tab.configure(image=ui.icon_image(
            'triangle_left' if shown else 'triangle_right',
            ui.TEXT_ON_DARK, 16))
        if not shown:
            # Clear the hover from the entry just clicked, so the rail
            # slides away looking at rest
            for button in self.nav_buttons:
                button._on_leave()
        self.slide_nav(shown)

    def slide_nav(self, shown):
        """Slide the navigation's window out or away over the page.

        Only the window moves; nothing in it or in the page is redrawn.
        """
        start, end = (-self.nav_width, 0) if shown else (0, -self.nav_width)
        began = time.perf_counter()

        def step():
            # Placed by the time gone rather than counted steps, so a slow
            # frame is caught up on rather than drawing the slide out
            done = min((time.perf_counter() - began) * 1000 / self.NAV_SLIDE_MS, 1.0)
            eased = 1 - (1 - done) ** 3   # quick away, settling gently
            self.place_nav(shown, round(start + (end - start) * eased))
            if done < 1.0:
                self.nav_slide = self.after(8, step)
            else:
                self.end_nav_slide()

        self.nav_slide = 'starting'
        step()

    def end_nav_slide(self):
        """Stop any slide and leave the navigation where it rests."""
        if self.nav_slide not in (None, 'starting'):
            self.after_cancel(self.nav_slide)
        self.nav_slide = None
        self.place_nav(self.nav_open)

    def nav_button(self, nav, icon, label, command, danger=False):
        button = NavButton(nav, icon, label, command, danger=danger)
        self.nav_buttons.append(button)
        return button

    def mark_current(self, entry):
        """Light up the rail entry whose console is on show, and only that one."""
        for button in self.nav_buttons:
            button.set_active(button is entry)

    def check_last_test_date(self):
        """Compare the clock against the last recorded test.

        Returns False when the machine should not be used - the system date
        is behind the last test, which would misdate everything saved next.
        Tests are saved with this PC's date, so that is the date compared,
        not the database server's.
        """
        try:
            conn = db.connect()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DATEDIFF(%s, MAX(TEST_DAY_DATE))
                FROM TBL_PART_RUNNING_SERIAL
            """, (datetime.today().date(),))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
        except Exception as e:
            # A machine that cannot reach the database has bigger problems,
            # but it should not be stopped from opening the console.
            print(f"Could not check the last test date: {e}")
            return True

        if not row or row[0] is None:
            return True  # nothing tested yet

        days = int(row[0])

        if days < 0:
            messagebox.showerror(
                "System Date Incorrect",
                "The SYSTEM Date is incorrect. Please consult Line Supervisor "
                "and Line Quality Incharge. The Application will close now."
            )
            return False

        if days > self.STALE_TEST_WARNING_DAYS:
            messagebox.showwarning(
                "Old Test Data",
                f"The Last Test Date is more than {self.STALE_TEST_WARNING_DAYS} "
                "days older. Please consult Line Supervisor and Line Quality "
                "Incharge before you proceed with the Testing."
            )

        return True

    def archive_folders(self):
        """The two configured archive folders, when both exist on disk."""
        first = config.get('PRIMARY_BACKUP_PATH', '')
        second = config.get('SECONDARY_BACKUP_PATH', '')

        if not first or not second:
            return None
        if not os.path.isdir(first) or not os.path.isdir(second):
            return None
        return [first, second]

    def load_settings(self):
        """Enable the consoles once the machine is configured for testing."""
        machine_id = config.get('MACHINE_ID', '')
        folders = self.archive_folders()
        ready = bool(machine_id) and folders is not None

        for btn in (self.btn_com_settings, self.btn_settings, self.btn_vision,
                    self.btn_test, self.btn_work_data):
            btn.set_enabled(ready)

        # Admin stays reachable, since that is where the machine ID and the
        # archive folders are set in the first place. Help and Contact are
        # left alone for the same reason: an unset machine is exactly when
        # someone needs to read what to set, or who to ring about it.
        self.btn_admin.set_enabled(True)

        if not ready:
            missing = []
            if not machine_id:
                missing.append("Machine ID")
            if folders is None:
                missing.append("both archive folders")
            self.after(300, lambda: messagebox.showwarning(
                "Setup Incomplete",
                "Testing is disabled until these are set in the Admin console:\n\n"
                + "\n".join(f"  - {item}" for item in missing)
            ))
            return

        # Let the window paint before touching the database.
        self.after(300, self.start_testing)

    def start_testing(self):
        """Do the start-up housekeeping, then open the test console.

        Testing is what the machine is for, so the application opens straight
        into that page instead of waiting for the operator to press Test.
        """
        self.archive_old_data()
        self.test_click()

    def archive_old_data(self):
        """Write records older than the retention window out to both folders."""
        folders = self.archive_folders()
        if folders is None:
            return

        try:
            conn = db.connect()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM TBL_TEST_DATA
                WHERE TD_DATETIME < DATE_SUB(CURDATE(), INTERVAL %s DAY)
            """, (self.ARCHIVE_AFTER_DAYS,))
            count = cursor.fetchone()[0] or 0

            if count == 0:
                cursor.close()
                conn.close()
                return

            cursor.execute("""
                SELECT * FROM TBL_TEST_DATA
                WHERE TD_DATETIME < DATE_SUB(CURDATE(), INTERVAL %s DAY)
                ORDER BY ID
            """, (self.ARCHIVE_AFTER_DAYS,))
            rows = cursor.fetchall()
            columns = [description[0] for description in cursor.description]
            cursor.close()
            conn.close()

        except Exception as e:
            print(f"Could not read records for archiving: {e}")
            return

        messagebox.showinfo(
            "Archive",
            f"There are {count} records older than {self.ARCHIVE_AFTER_DAYS} days."
        )

        written = self.export_to_csv(columns, rows, folders)
        if written:
            messagebox.showinfo(
                "Archive",
                "Archived {} records to:\n\n{}".format(count, "\n".join(written))
            )

    def export_to_csv(self, columns, rows, folders):
        """Write the rows to one CSV per archive folder; returns the paths written."""
        file_name = "{}_Archive_{}.csv".format(
            config.get('MACHINE_ID', 'MACHINE'),
            datetime.now().strftime("%m%d%Y_%I%M%S%p"))

        written = []
        for folder in folders:
            path = os.path.join(folder, file_name)
            try:
                with open(path, 'w', newline='', encoding='utf-8') as handle:
                    writer = csv.writer(handle)
                    writer.writerow([column.upper() for column in columns])
                    for row in rows:
                        writer.writerow(['' if value is None else value for value in row])
                written.append(path)
            except OSError as e:
                # One unreachable folder should not lose the other copy.
                print(f"Could not write archive to {folder}: {e}")
                messagebox.showwarning(
                    "Archive",
                    f"Could not write the archive to:\n{path}\n\n{e}"
                )

        return written

    def shell_is_alive(self):
        """False once the window and its panel are being torn down.

        Closing the window destroys its children one at a time, so the panel
        can still be there when the placeholder inside it has already gone.
        """
        try:
            return bool(self.page_host.winfo_exists()
                        and self.placeholder.winfo_exists())
        except tk.TclError:
            return False

    def show_placeholder(self):
        """What the page panel shows while no console is open."""
        if self.shell_is_alive():
            self.placeholder.pack(fill="both", expand=True)

    def open_page(self, title, build, on_closed=None, entry=None):
        """Build one console into the page panel, replacing what is there.

        `build` is handed the panel and returns the console instance,
        `on_closed` runs once that page has been torn down again, and
        `entry` is the rail entry to show as current while it is open.
        """
        self.close_page()
        self.placeholder.pack_forget()

        page = PageFrame(self.page_host, self, bg=ui.APP_BG)
        page.pack(fill="both", expand=True)
        self.page = page
        self.wm_title(f"EOL Tester - {title}")

        try:
            self.page_app = build(page)
        except Exception as e:
            print(f"Could not open the {title} page: {e}")
            self.page = None
            self.page_app = None
            page.shell = None  # half built, so nothing to report back
            page.destroy()
            self.wm_title("EOL Tester")
            self.show_placeholder()
            self.mark_current(None)
            messagebox.showerror(
                "EOL Tester",
                "The {} page could not be opened.\n\n{}".format(title, e))
            return False

        self.page_closed_hook = on_closed
        self.mark_current(entry)

        # The navigation lies over the page's left edge, so it is put away
        # a few seconds after a page is chosen; its tab brings it back.
        self.hide_nav_soon()
        return True

    def hide_nav_soon(self):
        """Put the navigation away once NAV_HIDE_DELAY_MS has passed."""
        if self.nav_hide_job is not None:
            self.after_cancel(self.nav_hide_job)
        self.nav_hide_job = self.after(self.NAV_HIDE_DELAY_MS, self.hide_nav_now)

    def hide_nav_now(self):
        self.nav_hide_job = None
        self.set_nav(False)

    def close_page(self):
        """Tear down the page on show, if there is one."""
        page, app, hook = self.page, self.page_app, self.page_closed_hook
        self.page = None
        self.page_app = None
        self.page_closed_hook = None

        if page is None:
            return

        # The console registered a close handler to release its ports and
        # stop its threads, so let it close by the path it expects.
        handler = page.close_handler
        if handler is not None:
            try:
                handler()
            except Exception as e:
                print(f"Error closing the page: {e}")
        elif app is not None and hasattr(app, 'cleanup'):
            try:
                app.cleanup()
            except Exception as e:
                print(f"Error cleaning up the page: {e}")

        if page.winfo_exists():
            page.shell = None  # already being torn down here
            page.destroy()

        self.wm_title("EOL Tester")
        self.show_placeholder()
        self.mark_current(None)
        self.run_closed_hook(hook)

    def page_closed(self, page):
        """A console that has just destroyed its own panel."""
        if page is not self.page:
            return  # already on its way out through close_page

        self.page = None
        self.page_app = None
        hook, self.page_closed_hook = self.page_closed_hook, None

        # Closing the window destroys its children too, and that comes
        # through here; there is then no panel left to put anything back in.
        if self.shell_is_alive():
            self.wm_title("EOL Tester")
            self.show_placeholder()
            self.mark_current(None)

        # The hook releases the PLC port, so it runs either way.
        self.run_closed_hook(hook)

    def run_closed_hook(self, hook):
        if hook is None:
            return
        try:
            hook()
        except Exception as e:
            print(f"Error after closing the page: {e}")

    def release_plc_port(self, when):
        """Force the PLC port shut, so the next connection can have it."""
        port = config.get('PLC_COM_PORT')
        if not port:
            return

        try:
            handle = serial.Serial(port)
            handle.close()
            print(f"MainConsole: released {port} {when}")
            # The port needs a moment before it can be opened again.
            time.sleep(1)
        except Exception as e:
            print(f"MainConsole: {port} is busy {when}: {e}")

    def com_port_settings_click(self):
        self.open_page("COM Port Settings", ComPortSettings,
                       entry=self.btn_com_settings)

    def settings_click(self):
        # Ask before tearing down the open page, so a cancelled login
        # leaves the operator on whatever they were already looking at.
        user = prompt_login(self)
        if user is None:
            return

        self.open_page("Model Settings",
                       lambda page: WorkspaceApp(page, user=user),
                       entry=self.btn_settings)

    def vision_click(self):
        # Teaching or deleting a part changes what passes on the line, so
        # this is behind a login like Settings.
        if prompt_login(self) is None:
            return

        self.open_page("Vision Settings", VisionSettings, entry=self.btn_vision)

    def test_click(self):
        self.open_page(
            "Test Console", self.build_test_console,
            on_closed=lambda: self.release_plc_port("after closing the test console"),
            entry=self.btn_test)

    def build_test_console(self, page):
        # A port left open by an earlier run would refuse to connect.
        self.release_plc_port("before opening the test console")
        return EOLTesterGUI(page)

    def work_data_click(self):
        self.open_page("Work Data", DataConsole, entry=self.btn_work_data)

    def admin_click(self):
        if prompt_login(self) is None:
            return

        self.open_page("Admin", AdminConsole, entry=self.btn_admin)

    def user_manual_click(self):
        self.open_page(
            "Help",
            lambda page: HelpConsole(page,
                                     archive_days=self.ARCHIVE_AFTER_DAYS,
                                     stale_days=self.STALE_TEST_WARNING_DAYS),
            entry=self.btn_help)

    def support_click(self):
        self.open_page("Contact", ContactConsole, entry=self.btn_contact)

    def exit_click(self):
        self.close_page()
        self.quit()

if __name__ == "__main__":
    app = MainConsole()
    app.mainloop()