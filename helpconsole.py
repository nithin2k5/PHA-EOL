"""Help and contact pages for the EOL Tester shell.

Both are built the way the other consoles are - handed a window or a panel
to fill - so the main console can open them in its page area like any
other page.
"""

import tkinter as tk

import config
import db
import ui

# Used when the machine has no SUPPORT_COMPANY of its own in .config.
DEFAULT_COMPANY = 'Nice Computers & Industrial Solutions'

# A contact nobody has filled in yet. Better to say so than to print an
# address or a number that goes nowhere.
NOT_SET = 'Not set yet - add it to the .config file'


class Page:
    """A title bar and footer around a scrolling body, shared by the two pages below.

    The body scrolls because these pages are read on whatever screen the
    line PC happens to have, and text that cannot be reached is no help.
    """

    TITLE = ''
    ICON = None

    def __init__(self, root):
        self.root = root
        ui.apply(root)
        self.root.title("EOL Tester - " + self.TITLE)

        # The pink title bar and footer every console carries; the footer is
        # packed before the scrolling body so it keeps the foot of the window
        ui.page_header(root, self.TITLE)
        ui.footer_bar(root)

        area = ui.scrollable(root)
        area.pack(fill='both', expand=True)

        self.build(area.body)

        # A little air under the last card.
        tk.Frame(area.body, bg=ui.APP_BG, height=ui.PAD_LARGE).pack(fill='x')

    def build(self, body):
        raise NotImplementedError

    def card(self, body, title, icon=None):
        """A titled rounded card down the page. Returns the frame to fill."""
        card = ui.ctk_card(body)
        card.pack(fill='x', padx=ui.PAD_LARGE, pady=(ui.PAD_LARGE, 0))
        ui.ctk_card_header(card, title, icon=icon)

        inner = tk.Frame(card, bg=ui.SURFACE)
        inner.pack(fill='both', expand=True, padx=ui.PAD_LARGE, pady=ui.PAD_LARGE)
        return inner

    def paragraph(self, parent, text, muted=False):
        label = tk.Label(parent, text=text, bg=ui.SURFACE,
                         fg=ui.TEXT_MUTED if muted else ui.TEXT,
                         font=ui.FONT_BODY, justify='left', anchor='w')
        label.pack(fill='x', pady=(0, ui.PAD))
        self.wrap_to_parent(parent, label)
        return label

    def rows(self, parent, pairs):
        """A term and its description per line, with the terms lined up."""
        table = tk.Frame(parent, bg=ui.SURFACE)
        table.pack(fill='x')
        table.grid_columnconfigure(1, weight=1)

        for row, (term, description) in enumerate(pairs):
            tk.Label(table, text=term, bg=ui.SURFACE, fg=ui.ACCENT,
                     font=ui.FONT_BODY_BOLD, anchor='nw').grid(
                         row=row, column=0, sticky='nw', pady=(0, ui.PAD))

            value = tk.Label(table, text=description, bg=ui.SURFACE,
                             fg=ui.TEXT, font=ui.FONT_BODY,
                             justify='left', anchor='nw')
            value.grid(row=row, column=1, sticky='nwe',
                       padx=(ui.PAD_LARGE, 0), pady=(0, ui.PAD))
            self.wrap_to_parent(table, value, reserve=180)
        return table

    def bullets(self, parent, items):
        for item in items:
            self.paragraph(parent, "  -  " + item)

    def wrap_to_parent(self, parent, label, reserve=0):
        """Keep a label's text wrapping to the width it actually has."""
        def resized(event):
            label.config(wraplength=max(event.width - reserve - 2 * ui.PAD, 200))

        parent.bind('<Configure>', resized, add='+')


class HelpConsole(Page):
    """What each console is for, and what has to be set before testing."""

    TITLE = 'Help'
    ICON = 'question'

    def __init__(self, root, archive_days=None):
        # Taken from the main console rather than repeated here, so the page
        # cannot quietly drift away from what the application actually does.
        self.archive_days = archive_days
        super().__init__(root)

    def build(self, body):
        setup = self.card(body, "Before testing can start", icon='box')
        self.paragraph(setup,
                       "A machine has to be identified and given somewhere to "
                       "archive to before it can be tested on. Until both are "
                       "set, COM Ports, Settings, Vision, Test and Work Data stay "
                       "greyed out in the navigation.")
        self.rows(setup, (
            ("Machine ID", "Identifies this machine on the test records."),
            ("Archive folders", "Two of them. Archived results are written to "
                                "both, so one unreachable folder does not lose "
                                "the only copy."),
        ))
        self.paragraph(setup,
                       "All three are set in the Admin console, which is why "
                       "Admin stays available when the others do not.",
                       muted=True)

        consoles = self.card(body, "The consoles", icon='list')
        self.rows(consoles, (
            ("COM Ports", "Serial ports for the PLC, load cells, LVDT, "
                          "cameras and scanners."),
            ("Settings", "The models each part number is tested against. "
                         "Asks for a login."),
            ("Vision", "Both cameras, each with a stripe check and an "
                       "object check: CAM1 - STRIPES, CAM1 - OBJ, CAM2 - "
                       "STRIPES and CAM2 - OBJ. Teach and test the parts "
                       "each one checks. A camera fails a part if any check "
                       "taught for it there fails. Asks for a login."),
            ("Test", "Runs a part through its test cycle. This is the console "
                     "the application opens into."),
            ("Work Data", "Search past results by part number, date and "
                          "result, and export them to CSV."),
            ("Admin", "Machine ID, archive folders and employee accounts. "
                      "Asks for a login."),
        ))

        login = self.card(body, "Signing in", icon='shield')
        self.paragraph(login,
                       "Settings, Vision and Admin ask for an employee number and "
                       "password. The other consoles open without one.")
        self.paragraph(login,
                       "Employee accounts are added and deactivated in the "
                       "Admin console. A deactivated account is refused at "
                       "the login.")

        startup = self.card(body, "When the application opens", icon='play')
        self.bullets(startup, (
            "The system date is checked against the last recorded test. If "
            "the clock is behind it, the application will not open, because "
            "everything saved next would be misdated.",
            self.archive_message(),
            "The Test console then opens by itself. Closing a page leaves you "
            "on the navigation, which stays on the left the whole time.",
        ))

    def archive_message(self):
        if self.archive_days is None:
            return ("Results past the retention window are written out to "
                    "both archive folders as a CSV.")
        return ("Results older than {} days are written out to both archive "
                "folders as a CSV.".format(self.archive_days))


class ContactConsole(Page):
    """Who to call, and the details worth having ready before calling."""

    TITLE = 'Contact'
    ICON = 'mail'

    def build(self, body):
        support = self.card(body, "Support", icon='mail')
        self.rows(support, (
            ("Company", config.get('SUPPORT_COMPANY', DEFAULT_COMPANY)),
            ("Email", config.get('SUPPORT_EMAIL', NOT_SET)),
            ("Phone", config.get('SUPPORT_PHONE', NOT_SET)),
        ))

        machine = self.card(body, "This machine", icon='gear')
        self.paragraph(machine, "Quote these when reporting a problem.")
        self.rows(machine, self.machine_details())

        ready = self.card(body, "Worth having ready", icon='clipboard')
        self.bullets(ready, (
            "The part number and lot number being tested.",
            "What the last step on screen was, and what the PLC was doing.",
            "Anything printed in the console window behind the application.",
        ))

    def machine_details(self):
        """Facts about this machine, with nothing secret among them."""
        details = [
            ("Machine ID", config.get('MACHINE_ID', 'Not set')),
            ("PLC port", config.get('PLC_COM_PORT', 'Not set')),
        ]

        # Host and database name only: the credentials stay out of a page
        # that anyone walking past the machine can read.
        try:
            database = db.get_config()
            details.append(("Database",
                            "{} on {}".format(database.get('database', '?'),
                                              database.get('host', '?'))))
        except Exception as e:
            details.append(("Database", "Could not be read: {}".format(e)))

        details.append(("Settings file", config.CONFIG_FILE))
        return details
