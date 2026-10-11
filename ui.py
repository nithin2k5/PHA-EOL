"""Shared look and feel for every console.

The palette is the Test console's, which operators know from the line:
white panels on window grey, a navy accent, sky-blue header bands and fills
with black text, Cambria type, and colour reserved for meaning - green
passes, red failures, orange warnings. Each page carries the same pink
title bar (title_bar / page_header).

Pages were written with their own colour literals scattered through them,
so rather than rewriting thousands of call sites this module translates
those literals to palette tokens as widgets are built, and guarantees that
whatever background a control ends up with, its text stays readable on it.

    import ui
    ui.apply(root)
"""

import os
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

import config
import icons

# customtkinter defaults to following the OS light/dark setting, which would
# make its rounded widgets drift from this module's fixed light palette.
ctk.set_appearance_mode("light")

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------

APP_BG = '#F0F0F0'      # window grey behind the panels
SURFACE = '#FFFFFF'     # panels, grids and input wells
SUBTLE = '#E8E8E8'      # caption strips, neutral buttons
BORDER = '#A0A0A0'
BORDER_STRONG = '#808080'

TEXT = '#000000'
TEXT_MUTED = '#555555'
# Text on the sky-blue accent fill, as on the Test console's lamps and grid
# headers. Black reads far better on that bright blue than white does.
TEXT_ON_ACCENT = '#000000'
# The two options `readable_on` chooses between.
TEXT_ON_LIGHT = '#000000'
TEXT_ON_DARK = '#FFFFFF'

# The accent in two roles: navy for text, icons and headers on light
# panels, sky blue for filled buttons and header bands.
ACCENT = '#191970'          # MidnightBlue
ACCENT_FILL = '#00BFFF'     # DeepSkyBlue, carrying TEXT_ON_ACCENT
ACCENT_HOVER = '#33CCFF'
ACCENT_ACTIVE = '#009ACD'

# Tinted backgrounds, for marking a row as selected without filling it with
# the full accent.
ACCENT_SOFT = '#CCE8FF'
DANGER_SOFT = '#FFD6D6'
SUCCESS_SOFT = '#D6F5D6'

DISABLED_BG = '#E0E0E0'
DISABLED_TEXT = '#8C8C8C'

SUCCESS = '#008000'
SUCCESS_HOVER = '#006400'
DANGER = '#E00000'
DANGER_HOVER = '#B00000'
WARNING = '#FF8C00'
WARNING_HOVER = '#E07B00'

# Row shading for result grids: bands of five in light yellow
ROW_BAND = '#FFFF99'
ROW_PLAIN = SURFACE

# The Test console's own colours, for the bars and boxes pages share with it
TITLE_PINK = '#FFB6C1'      # LightPink title bar
FOOTER_PINK = '#FFC0CB'     # Pink footer
NAVY = ACCENT               # part and page header bands
SKY = ACCENT_FILL           # idle step lamps, grid headers
AQUA = '#00FFFF'            # code entry boxes
YELLOW = '#FFFF00'          # the next-model button, employee code box
SILVER = '#C0C0C0'
POWDER = '#B0E0E6'          # counter strips
LAMP_PASS = '#00FF00'       # a passed step
LAMP_FAIL = '#FF4500'       # a failed step

# --------------------------------------------------------------------------
# Chart tokens
# --------------------------------------------------------------------------
#
# Trend charts are drawn black with a dash-dot grid and white labels, as on
# the Test console. SERIES is assigned per channel in fixed order and never
# cycled, so L1 and P1 keep their colour on every chart.
CHART_SURFACE = '#000000'
CHART_GRID = '#DCDCDC'       # Gainsboro
CHART_AXIS = '#DCDCDC'
CHART_TEXT = '#FFFFFF'
SERIES = ('#418CF0', '#FCB441', '#E0400A', '#056492')

# Type scale
FONT_FAMILY = 'Cambria'
FONT_BODY = (FONT_FAMILY, 11)
FONT_BODY_BOLD = (FONT_FAMILY, 11, 'bold')
FONT_SMALL = (FONT_FAMILY, 10)
FONT_SECTION = (FONT_FAMILY, 12, 'bold')
FONT_TITLE = (FONT_FAMILY, 30, 'bold')

# Spacing step, so padding is consistent rather than ad hoc
PAD = 6
PAD_LARGE = 12

# --------------------------------------------------------------------------
# Translating the colour literals already in the pages
# --------------------------------------------------------------------------

# Colours that carry meaning keep it; neutrals and the old accent literals
# land on the palette, so a page written with its own colour choices still
# comes out in the Test console's scheme.
_BACKGROUND_MAP = {
    # neutrals
    'white': SURFACE, '#ffffff': SURFACE, '#fff': SURFACE,
    '#f0f0f0': APP_BG, '#f5f5f5': APP_BG, '#f8f9fa': APP_BG,
    '#e0e0e0': SUBTLE, '#e8e8e8': SUBTLE, 'lightgray': SUBTLE,
    'lightgrey': SUBTLE, 'gray': SUBTLE, 'grey': SUBTLE,
    '#2b2b2b': SURFACE,
    '#f5e6e8': SURFACE, '#e8f6e9': SURFACE, '#e6eef5': SURFACE,
    '#f5f0e6': SURFACE,
    'lightyellow': ROW_BAND, '#fff9c4': ROW_BAND, '#fef9c3': ROW_BAND,
    '#ffff99': ROW_BAND,
    # the Test console's bars
    'pink': FOOTER_PINK, 'lightpink': TITLE_PINK, '#ffb6c1': TITLE_PINK,
    # accent family: navy stays a dark band, the blues become the sky fill
    'navy': NAVY, 'darkblue': NAVY, '#2c3e50': NAVY, 'midnightblue': NAVY,
    'blue': ACCENT_FILL, 'deepskyblue': ACCENT_FILL, '#00bfff': ACCENT_FILL,
    '#1e88e5': ACCENT_FILL, '#3498db': ACCENT_FILL, '#2980b9': ACCENT_ACTIVE,
    '#0d6efd': ACCENT_FILL, '#add8e6': ACCENT_SOFT, 'lightblue': ACCENT_SOFT,
    '#cce5ff': ACCENT_SOFT, '#e6f2ff': ACCENT_SOFT,
    'aqua': AQUA, 'cyan': AQUA, 'powderblue': POWDER,
    # success family
    'green': SUCCESS, '#2ecc71': SUCCESS, '#27ae60': SUCCESS_HOVER,
    '#4caf50': SUCCESS, '#198754': SUCCESS, '#45a049': SUCCESS_HOVER,
    'lightgreen': SUCCESS_SOFT, '#90ee90': SUCCESS_SOFT,
    # danger family
    'red': DANGER, 'darkred': DANGER, '#e74c3c': DANGER,
    '#c0392b': DANGER_HOVER, '#f44336': DANGER, '#ff4d4d': DANGER,
    '#dc3545': DANGER, '#ff3333': DANGER, '#ff0000': DANGER,
    '#ffcccb': DANGER_SOFT, '#ffe6e6': DANGER_SOFT,
    # warning family; plain yellow is the Test console's yellow
    'yellow': YELLOW, 'orange': WARNING, '#ffeb3b': YELLOW,
    # purple used for the edit action
    '#9b59b6': ACCENT_FILL, '#8e44ad': ACCENT_ACTIVE,
    '#95a5a6': SILVER, '#7f8c8d': BORDER_STRONG,
}

# Fills with a designated text colour, whichever way the contrast maths
# would otherwise fall: black on the sky-blue fills, white on navy.
_FILL_TEXT = {
    NAVY: TEXT_ON_DARK,
    ACCENT_FILL: TEXT_ON_ACCENT,
    ACCENT_HOVER: TEXT_ON_ACCENT,
    ACCENT_ACTIVE: TEXT_ON_ACCENT,
}

_FOREGROUND_MAP = {
    'white': TEXT_ON_DARK, '#ffffff': TEXT_ON_DARK,
    'black': TEXT, '#000000': TEXT, '#1a1a1a': TEXT, '#2b2b2b': TEXT,
    '#333333': TEXT, '#424242': TEXT, '#2c3e50': ACCENT,
    '#999999': TEXT_MUTED, '#666666': TEXT_MUTED,
    'gray': TEXT_MUTED, 'grey': TEXT_MUTED, 'lightgray': TEXT_MUTED,
    'green': SUCCESS, '#00ff00': SUCCESS, '#4caf50': SUCCESS,
    '#198754': SUCCESS, '#2ecc71': SUCCESS, '#27ae60': SUCCESS,
    'red': DANGER, 'darkred': DANGER, '#ff0000': DANGER,
    'navy': ACCENT, 'blue': ACCENT, 'darkblue': ACCENT,
    'orange': WARNING, 'yellow': WARNING,
}

# Backgrounds passed through untouched even if they appear above.
_KEEP_BACKGROUND = set()

_NAMED_RGB = {
    'white': (255, 255, 255), 'black': (0, 0, 0), 'red': (255, 0, 0),
    'green': (0, 128, 0), 'blue': (0, 0, 255), 'navy': (0, 0, 128),
    'darkred': (139, 0, 0), 'darkblue': (0, 0, 139), 'orange': (255, 165, 0),
    'yellow': (255, 255, 0), 'pink': (255, 192, 203), 'gray': (128, 128, 128),
    'grey': (128, 128, 128), 'lightgray': (211, 211, 211),
    'lightgrey': (211, 211, 211), 'deepskyblue': (0, 191, 255),
    'lightyellow': (255, 255, 224), 'cyan': (0, 255, 255),
}


def _rgb(color):
    """Best effort RGB for a colour literal, or None when unrecognised."""
    if not isinstance(color, str) or not color:
        return None

    value = color.strip().lower()
    if value in _NAMED_RGB:
        return _NAMED_RGB[value]

    if value.startswith('#'):
        digits = value[1:]
        if len(digits) == 3:
            digits = ''.join(c * 2 for c in digits)
        if len(digits) == 6:
            try:
                return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                return None
    return None


def _channel(value):
    """One sRGB channel converted to linear light."""
    value = value / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(color):
    """Relative luminance, gamma corrected the way contrast ratios expect."""
    rgb = _rgb(color)
    if rgb is None:
        return None
    red, green, blue = (_channel(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _ratio(first, second):
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def readable_on(background, dark=TEXT_ON_LIGHT, light=TEXT_ON_DARK):
    """Pick whichever text colour reads better on this background.

    `dark` is the option for light backgrounds and `light` the option for
    dark ones.
    """
    base = _luminance(background)
    if base is None:
        return dark

    dark_luminance = _luminance(dark)
    light_luminance = _luminance(light)
    if dark_luminance is None or light_luminance is None:
        return dark

    return dark if _ratio(base, dark_luminance) >= _ratio(base, light_luminance) else light


def mix(first, second, amount=0.5):
    """Blend two colours, amount 0 gives the first and 1 the second."""
    left, right = _rgb(first), _rgb(second)
    if left is None or right is None:
        return second
    blended = tuple(round(a + (b - a) * amount) for a, b in zip(left, right))
    return '#%02x%02x%02x' % blended


def _has_contrast(foreground, background):
    """True when these two are far enough apart to read."""
    first = _luminance(foreground)
    second = _luminance(background)
    if first is None or second is None:
        return True  # unknown colour, leave the author's choice alone
    return _ratio(first, second) >= 3.0


def map_background(color):
    if not isinstance(color, str):
        return color
    value = color.strip().lower()
    if value in _KEEP_BACKGROUND:
        return color
    return _BACKGROUND_MAP.get(value, color)


def map_foreground(color):
    if not isinstance(color, str):
        return color
    return _FOREGROUND_MAP.get(color.strip().lower(), color)


# --------------------------------------------------------------------------
# ttk styles
# --------------------------------------------------------------------------

def _configure_ttk():
    style = ttk.Style()
    if 'clam' in style.theme_names():
        style.theme_use('clam')

    style.configure('.',
                    background=APP_BG, foreground=TEXT,
                    fieldbackground=SURFACE, bordercolor=BORDER,
                    lightcolor=APP_BG, darkcolor=APP_BG,
                    troughcolor=SUBTLE, font=FONT_BODY,
                    selectbackground=ACCENT_FILL, selectforeground=TEXT_ON_ACCENT)

    style.configure('TFrame', background=APP_BG)
    style.configure('Surface.TFrame', background=SURFACE)
    style.configure('TLabel', background=APP_BG, foreground=TEXT, font=FONT_BODY)
    style.configure('Muted.TLabel', foreground=TEXT_MUTED, font=FONT_SMALL)
    style.configure('Title.TLabel', font=FONT_TITLE, foreground=TEXT)
    style.configure('Section.TLabel', font=FONT_SECTION, foreground=ACCENT)

    style.configure('TLabelframe', background=SURFACE,
                    bordercolor=BORDER, borderwidth=1, relief='solid')
    style.configure('TLabelframe.Label', background=SURFACE,
                    foreground=ACCENT, font=FONT_SECTION)

    style.configure('TButton', padding=(PAD_LARGE, PAD), relief='flat',
                    background=ACCENT_FILL, foreground=TEXT_ON_ACCENT,
                    font=FONT_BODY_BOLD, borderwidth=0)
    style.map('TButton',
              background=[('disabled', DISABLED_BG),
                          ('pressed', ACCENT_ACTIVE),
                          ('active', ACCENT_HOVER)],
              foreground=[('disabled', DISABLED_TEXT)])

    for name, base, hover in (('Success', SUCCESS, SUCCESS_HOVER),
                              ('Danger', DANGER, DANGER_HOVER),
                              ('Warning', WARNING, WARNING_HOVER),
                              ('Neutral', SUBTLE, BORDER)):
        text_colour = readable_on(base)
        style.configure(f'{name}.TButton', background=base, foreground=text_colour)
        style.map(f'{name}.TButton',
                  background=[('disabled', DISABLED_BG), ('active', hover)],
                  foreground=[('disabled', DISABLED_TEXT), ('active', readable_on(hover))])

    style.configure('TEntry', fieldbackground=SURFACE, foreground=TEXT,
                    bordercolor=BORDER, insertcolor=TEXT, padding=4)
    style.map('TEntry',
              fieldbackground=[('disabled', DISABLED_BG), ('readonly', SUBTLE)],
              foreground=[('disabled', DISABLED_TEXT)])

    style.configure('TCombobox', fieldbackground=SURFACE, foreground=TEXT,
                    bordercolor=BORDER, arrowcolor=TEXT_MUTED,
                    selectbackground=SURFACE, selectforeground=TEXT, padding=4)
    # Most of this app's comboboxes are state='readonly', which has its own
    # field colour - left alone it stays the system light grey.
    style.map('TCombobox',
              fieldbackground=[('readonly', SURFACE), ('disabled', DISABLED_BG)],
              foreground=[('readonly', TEXT), ('disabled', DISABLED_TEXT)],
              selectbackground=[('readonly', SURFACE)],
              selectforeground=[('readonly', TEXT)],
              background=[('readonly', SUBTLE), ('active', SUBTLE)],
              arrowcolor=[('disabled', DISABLED_TEXT)])
    # The drop-down itself is a Tk listbox rather than a ttk widget, so it
    # only answers to the option database.
    for pattern, value in (('*TCombobox*Listbox.background', SURFACE),
                           ('*TCombobox*Listbox.foreground', TEXT),
                           ('*TCombobox*Listbox.selectBackground', ACCENT_SOFT),
                           ('*TCombobox*Listbox.selectForeground', ACCENT)):
        try:
            style.master.option_add(pattern, value)
        except (AttributeError, tk.TclError):
            pass

    style.configure('Treeview', background=SURFACE, fieldbackground=SURFACE,
                    foreground=TEXT, rowheight=26, borderwidth=1,
                    bordercolor=BORDER, font=FONT_BODY)
    # A filled header band, so the column names read as a header rather
    # than as a slightly different first row. Every grid inherits this;
    # the dotted style names below it derive from this one.
    style.configure('Treeview.Heading', background=ACCENT_FILL,
                    foreground=TEXT_ON_ACCENT, font=FONT_BODY_BOLD,
                    relief='flat', borderwidth=0, padding=(PAD, PAD))
    style.map('Treeview',
              background=[('selected', ACCENT_SOFT)],
              foreground=[('selected', TEXT)])
    style.map('Treeview.Heading', background=[('active', ACCENT_HOVER)])

    style.configure('TNotebook', background=APP_BG, tabmargins=[2, 5, 2, 0])
    style.configure('TNotebook.Tab', background=SUBTLE, foreground=TEXT,
                    padding=[PAD_LARGE, PAD], font=FONT_BODY_BOLD)
    style.map('TNotebook.Tab',
              background=[('selected', ACCENT_FILL)],
              foreground=[('selected', TEXT_ON_ACCENT)])

    # Both orientations have to be named: clam draws them through
    # Horizontal./Vertical. prefixed styles, which do not inherit the
    # colours set on the bare TScrollbar name.
    for scrollbar in ('TScrollbar', 'Horizontal.TScrollbar', 'Vertical.TScrollbar'):
        style.configure(scrollbar, background=BORDER, troughcolor=APP_BG,
                        bordercolor=APP_BG, arrowcolor=TEXT_MUTED,
                        darkcolor=SURFACE, lightcolor=SURFACE,
                        gripcount=0, relief='flat')
        style.map(scrollbar,
                  background=[('active', BORDER_STRONG),
                              ('disabled', APP_BG)],
                  arrowcolor=[('disabled', DISABLED_TEXT)])
    return style


# --------------------------------------------------------------------------
# Normalising the classic tk widgets
# --------------------------------------------------------------------------

_BUTTON_LIKE = ('Button', 'Checkbutton', 'Radiobutton')

# Frames, canvases and toplevels have no foreground option at all, so text
# colour is only ever set on the widgets that actually draw text.
_HAS_FOREGROUND = ('Label', 'Button', 'Checkbutton', 'Radiobutton', 'Entry',
                   'Text', 'Listbox', 'Spinbox', 'LabelFrame', 'Message')

_patched = False


def _normalise(widget_class, options):
    """Translate colour options and make sure the text stays readable."""
    name = widget_class.__name__

    background = options.get('bg', options.get('background'))
    foreground = options.get('fg', options.get('foreground'))

    if isinstance(background, str):
        background = map_background(background)
        options.pop('background', None)
        options['bg'] = background

    takes_foreground = name in _HAS_FOREGROUND

    if isinstance(foreground, str):
        foreground = map_foreground(foreground)
        options.pop('foreground', None)
        if takes_foreground:
            options['fg'] = foreground
        else:
            options.pop('fg', None)

    # A control with a background but no readable text is the whole reason
    # this exists: give it one, or replace one that cannot be read.
    if takes_foreground and isinstance(background, str):
        designated = _FILL_TEXT.get(background)
        if designated is not None:
            options['fg'] = designated
        elif not isinstance(foreground, str) or not _has_contrast(foreground, background):
            options['fg'] = readable_on(background)

    if name in _BUTTON_LIKE and isinstance(background, str):
        text_colour = options.get('fg', readable_on(background))
        options.setdefault('activebackground', mix(background, text_colour, 0.15))
        options.setdefault('activeforeground', text_colour)
        options.setdefault('relief', 'flat')
        options.setdefault('borderwidth', 0)
        options.setdefault('cursor', 'hand2')
        # Tk paints a disabled label in a system grey that disappears against
        # a saturated background, so fade the real text colour instead.
        options.setdefault('disabledforeground', mix(background, text_colour, 0.45))

    # Input wells get a visible edge; without a background of their own they
    # inherit a system colour and all but vanish against a white panel.
    if name in ('Entry', 'Text', 'Listbox', 'Spinbox') and not isinstance(background, str):
        options.setdefault('bg', SURFACE)
        options.setdefault('relief', 'solid')
        options.setdefault('borderwidth', 1)
        options.setdefault('highlightthickness', 1)
        options.setdefault('highlightbackground', BORDER)
        options.setdefault('highlightcolor', ACCENT)
        # A listbox has no text cursor, and Tk refuses the option outright.
        if name != 'Listbox':
            options.setdefault('insertbackground', TEXT)

    # A disabled or readonly Entry ignores `bg` entirely and paints itself in
    # a system light grey, which on a dark panel reads as a blank white slab.
    # Mirror whatever background it is being given into those two states, so
    # a page that colours an entry to mean something (a validated code going
    # green, say) keeps that meaning once the entry is locked.
    if name in ('Entry', 'Spinbox'):
        well = options.get('bg', SURFACE)
        options.setdefault('disabledbackground', well)
        options.setdefault('readonlybackground', well)
        options.setdefault('disabledforeground', TEXT_MUTED)

    # Classic scrollbars and sliders paint their trough and thumb from
    # their own options rather than from the background they sit on, so
    # left alone they stay a pale system grey against a dark page.
    if name in ('Scrollbar', 'Scale'):
        options.setdefault('bg', SUBTLE)
        options.setdefault('troughcolor', APP_BG)
        options.setdefault('activebackground', BORDER_STRONG)
        options.setdefault('highlightthickness', 0)
        options.setdefault('borderwidth', 0)
        if name == 'Scrollbar':
            options.setdefault('elementborderwidth', 0)

    if name == 'Menu':
        options.setdefault('bg', SURFACE)
        options.setdefault('fg', TEXT)
        options.setdefault('activebackground', ACCENT_SOFT)
        options.setdefault('activeforeground', ACCENT)
        options.setdefault('borderwidth', 0)

    return options


def _patch_tk_widgets():
    """Route classic tk widget colours through the palette, once."""
    global _patched
    if _patched:
        return
    _patched = True

    # tk.Tk is deliberately absent: its constructor takes screenName and
    # baseName rather than (master, cnf), so wrapping it breaks any window
    # created after the patch is installed.
    for widget_class in (tk.Frame, tk.LabelFrame, tk.Label, tk.Button,
                         tk.Checkbutton, tk.Radiobutton, tk.Entry, tk.Text,
                         tk.Listbox, tk.Canvas, tk.Spinbox, tk.Toplevel,
                         tk.Scrollbar, tk.Menu, tk.Scale):
        _patch_one(widget_class)


def _patch_one(widget_class):
    original_init = widget_class.__init__
    original_configure = widget_class.configure

    def patched_init(self, master=None, cnf=None, **kw):
        if cnf:
            kw = dict(cnf, **kw)
        # exact_colors=True opts a widget, and everything built inside it,
        # out of the palette: a screen drawn to a fixed colour scheme keeps
        # the colours it asks for rather than having them themed.
        exact = kw.pop('exact_colors', None)
        if exact is None:
            exact = getattr(master, '_exact_colors', False)
        # ttk.Entry and ttk.Combobox subclass tkinter.Entry, but they are
        # styled through ttk.Style and reject per-widget colour options.
        if not isinstance(self, ttk.Widget) and not exact:
            kw = _normalise(widget_class, kw)
        original_init(self, master, {}, **kw)
        self._exact_colors = exact

    def patched_configure(self, cnf=None, **kw):
        if cnf and isinstance(cnf, dict):
            kw = dict(cnf, **kw)
        elif cnf is not None:
            # A plain string means "read this option back"; leave it alone.
            return original_configure(self, cnf, **kw)
        if not kw:
            return original_configure(self)
        if not isinstance(self, ttk.Widget) and not getattr(self, '_exact_colors', False):
            kw = _normalise(widget_class, kw)
        return original_configure(self, **kw)

    widget_class.__init__ = patched_init
    widget_class.configure = patched_configure
    widget_class.config = patched_configure


class PaddedEntry(tk.Entry):
    """A classic Entry with a little room between its text and its edge.

    The palette gives every Entry a one-pixel solid edge, and gives it again
    on each configure call made without a colour, so text sits hard against
    the border. This one keeps a flat border painted in the entry's own
    colour inside a one-pixel outline, through every later configure call,
    so the text stays inset whatever state or colour the page sets. At
    PAD = 4 it stands as tall as a themed combobox, so the two line up.
    """

    PAD = 4

    def __init__(self, master=None, cnf=None, **kw):
        super().__init__(master, **self._padded(dict(cnf or {}, **kw)))

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str) or (cnf is None and not kw):
            return super().configure(cnf, **kw)    # reading an option back
        return super().configure(**self._padded(dict(cnf or {}, **kw)))

    config = configure

    @classmethod
    def _padded(cls, options):
        options.setdefault('relief', 'flat')
        options.setdefault('borderwidth', cls.PAD)
        options.setdefault('highlightthickness', 1)
        options.setdefault('highlightbackground', BORDER)
        options.setdefault('highlightcolor', ACCENT)
        return options


def apply(root):
    """Give this window the shared look. Safe to call from every page."""
    _patch_tk_widgets()
    style = _configure_ttk()

    try:
        root.configure(bg=APP_BG)
    except tk.TclError:
        pass

    # Widgets built without any colour of their own fall back to Tk's own
    # system defaults, which are light - on a dark theme those show up as
    # pale grey slabs in the middle of a page. The option database gives
    # them a dark default instead, while anything that asks for a specific
    # colour still wins.
    defaults = (
        ('*Font', FONT_BODY),
        ('*Background', APP_BG),
        ('*Foreground', TEXT),
        ('*selectBackground', ACCENT_SOFT),
        ('*selectForeground', TEXT),
        ('*troughColor', SUBTLE),
        ('*highlightBackground', APP_BG),
        ('*highlightColor', BORDER),
        ('*Entry.background', SURFACE),
        ('*Text.background', SURFACE),
        ('*Listbox.background', SURFACE),
        ('*Menu.background', SURFACE),
        ('*Menu.foreground', TEXT),
        ('*Menu.activeBackground', ACCENT_SOFT),
        ('*Menu.activeForeground', ACCENT),
    )
    for pattern, value in defaults:
        try:
            root.option_add(pattern, value)
        except tk.TclError:
            pass

    return style


# --------------------------------------------------------------------------
# Small building blocks pages can use directly
# --------------------------------------------------------------------------

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'assets', 'infac_india.png')
_logo_images = {}


def _logo(max_size):
    """The INFAC INDIA logo as a Tk image, cached so it is not collected."""
    if max_size not in _logo_images:
        from PIL import Image, ImageTk
        image = Image.open(LOGO_PATH)
        image.thumbnail(max_size)
        _logo_images[max_size] = ImageTk.PhotoImage(image)
    return _logo_images[max_size]


def title_bar(parent, title, right_text=None, height=60, font_size=30):
    """The Test console's pink title bar: logo, centred title, machine ID.

    `right_text` defaults to the machine ID, so photos and reports of any
    screen say which line they came from.
    """
    if right_text is None:
        right_text = config.get('MACHINE_ID', '')

    bar = tk.Frame(parent, bg=TITLE_PINK, height=height, exact_colors=True)
    bar.pack(fill='x', padx=3, pady=(3, 0))
    bar.pack_propagate(False)

    try:
        tk.Label(bar, image=_logo((150, height - 6)), bg=TITLE_PINK).pack(
            side='left', padx=(8, 0))
    except Exception as e:
        print(f"Logo not shown: {e}")

    if right_text:
        tk.Label(bar, text=right_text, bg=TITLE_PINK, fg=TEXT,
                 font=(FONT_FAMILY, max(12, font_size - 6), 'bold')).pack(
                     side='right', padx=12)

    tk.Label(bar, text=title, bg=TITLE_PINK, fg=TEXT,
             font=(FONT_FAMILY, font_size, 'bold')).place(
                 relx=0.5, rely=0.5, anchor='center')
    return bar


FOOTER_TEXT = "Powered By: NICE COMPUTERS AND SOFTWARE SOLUTIONS, Kavali, A.P"


def footer_bar(parent, text=FOOTER_TEXT):
    """The Test console's pink footer strip, for pages that sign off at the foot."""
    bar = tk.Frame(parent, bg=FOOTER_PINK, height=34, exact_colors=True)
    bar.pack(fill='x', side='bottom', padx=3, pady=3)
    bar.pack_propagate(False)
    tk.Label(bar, text=text, bg=FOOTER_PINK, fg=TEXT,
             font=(FONT_FAMILY, 11, 'bold')).pack(side='left', padx=8)
    return bar


def page_header(parent, title, right_text=None, compact=False, icon=None):
    """A page's title bar, the same pink bar the Test console carries.

    `compact` gives a shorter bar with smaller type. `icon` is accepted for
    older callers and no longer drawn: the logo takes that place.
    """
    if compact:
        return title_bar(parent, title.upper(), right_text, height=44, font_size=20)
    return title_bar(parent, title.upper(), right_text)


def section(parent, title, icon='', **kwargs):
    """A titled panel with a navy caption band. Returns the frame to fill.

    `icon` is a single glyph shown before the title.
    """
    outer = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER,
                     highlightthickness=1, **kwargs)

    caption = tk.Frame(outer, bg=NAVY)
    caption.pack(fill='x')

    if icon:
        tk.Label(caption, text=icon, bg=NAVY, fg=TEXT_ON_DARK,
                 font=FONT_SECTION).pack(side='left', padx=(PAD, 0), pady=PAD)

    tk.Label(caption, text=title, bg=NAVY, fg=TEXT_ON_DARK,
             font=FONT_SECTION, anchor='w', padx=PAD, pady=PAD).pack(
                 side='left', fill='x', expand=True)

    body = tk.Frame(outer, bg=SURFACE)
    body.pack(fill='both', expand=True, padx=PAD, pady=PAD)

    outer.body = body
    outer.caption = caption
    return outer


def empty_state(parent, icon, title, subtitle='', bg=SURFACE):
    """A centred "nothing here yet" block: a glyph, a title and a subtitle.

    Returns the frame; callers that want it centred in an already-expanding
    parent should pack/place it with expand=True themselves.
    """
    holder = tk.Frame(parent, bg=bg)

    tk.Label(holder, text=icon, bg=bg, fg=BORDER_STRONG,
            font=(FONT_FAMILY, 28)).pack(pady=(0, PAD))
    tk.Label(holder, text=title, bg=bg, fg=TEXT_MUTED,
            font=FONT_BODY_BOLD).pack()
    if subtitle:
        tk.Label(holder, text=subtitle, bg=bg, fg=TEXT_MUTED,
                font=FONT_SMALL).pack(pady=(2, 0))

    return holder


def scrollable(parent, bg=APP_BG, horizontal=False):
    """A scrolling area. Returns the container; fill `container.body`.

    Pages were laid out at fixed pixel sizes, so on a smaller screen the
    lower rows and right-hand panels simply fall off and cannot be reached.
    Putting the body in one of these keeps every control available.
    """
    container = tk.Frame(parent, bg=bg)
    canvas = tk.Canvas(container, bg=bg, highlightthickness=0)
    inner = tk.Frame(canvas, bg=bg)

    window = canvas.create_window((0, 0), window=inner, anchor='nw')

    vbar = ttk.Scrollbar(container, orient='vertical', command=canvas.yview)
    canvas.configure(yscrollcommand=vbar.set)

    hbar = None
    if horizontal:
        hbar = ttk.Scrollbar(container, orient='horizontal', command=canvas.xview)
        canvas.configure(xscrollcommand=hbar.set)

    def on_inner_configure(event):
        canvas.configure(scrollregion=canvas.bbox('all'))

    def on_canvas_configure(event):
        # Stretching the body to the canvas is what lets a narrow page fill
        # the width; a page that scrolls sideways must keep its own width.
        if not horizontal:
            canvas.itemconfigure(window, width=event.width)
        elif event.width > inner.winfo_reqwidth():
            canvas.itemconfigure(window, width=event.width)

    inner.bind('<Configure>', on_inner_configure)
    canvas.bind('<Configure>', on_canvas_configure)

    def on_wheel(event):
        if canvas.winfo_exists():
            canvas.yview_scroll(-1 * (event.delta // 120), 'units')

    canvas.bind_all('<MouseWheel>', on_wheel, add='+')

    # Grid, so both bars sit outside the canvas without overlapping it.
    canvas.grid(row=0, column=0, sticky='nsew')
    vbar.grid(row=0, column=1, sticky='ns')
    if hbar is not None:
        hbar.grid(row=1, column=0, sticky='ew')
    container.grid_rowconfigure(0, weight=1)
    container.grid_columnconfigure(0, weight=1)

    container.body = inner
    container.canvas = canvas
    return container


# --------------------------------------------------------------------------
# Rounded widgets (customtkinter), for the pages redesigned toward rounded
# cards and real icons
# --------------------------------------------------------------------------
#
# Classic tk can only draw flat rectangles, so everything above translates
# colour literals rather than round anything. Where a page specifically
# wants the rounded look, these build on customtkinter instead - a thin
# skin over Tk with real rounded corners and real icon images. Its widgets
# are still ordinary Tk widgets underneath, so they mix freely with a plain
# tk.Tk root and classic tk siblings; nothing here requires the whole app,
# or even the whole page, to move over at once.

CORNER_RADIUS = 14
CORNER_RADIUS_SMALL = 10

# fg_color, hover_color per semantic "kind" - the same meanings as the
# palette above (primary action, success, failure, needs-attention).
_BUTTON_KINDS = {
    'primary': (ACCENT_FILL, ACCENT_HOVER),
    'success': (SUCCESS, SUCCESS_HOVER),
    'danger': (DANGER, DANGER_HOVER),
    'warning': (WARNING, WARNING_HOVER),
    'neutral': (SUBTLE, BORDER),
}


def icon_image(name, color, size=28):
    """A cached icon image (see icons.py), ready for a CTk `image=` option."""
    return icons.ctk_image(name, color, size=size)


def ctk_card(parent, **kwargs):
    """A rounded white card, matching the reference design's panels."""
    kwargs.setdefault('corner_radius', CORNER_RADIUS)
    kwargs.setdefault('fg_color', SURFACE)
    kwargs.setdefault('border_width', 1)
    kwargs.setdefault('border_color', BORDER)
    return ctk.CTkFrame(parent, **kwargs)


def ctk_card_header(card, title, icon=None, height=34):
    """The navy icon+title strip along the top of a ctk_card.

    Inset a couple of pixels from the card's own edge, so the card's
    rounded corners stay visible around it rather than being squared off
    by a banner running edge to edge.
    """
    header = ctk.CTkFrame(card, corner_radius=CORNER_RADIUS_SMALL,
                          fg_color=NAVY, height=height)
    header.pack(fill='x', padx=6, pady=(6, 0))
    header.pack_propagate(False)

    if icon:
        ctk.CTkLabel(header, text='', image=icon_image(icon, TEXT_ON_DARK, 18),
                    fg_color=NAVY, width=18).pack(side='left', padx=(PAD_LARGE, 0))

    ctk.CTkLabel(header, text=title, fg_color=NAVY, text_color=TEXT_ON_DARK,
                font=FONT_SECTION).pack(side='left', padx=PAD)
    return header


def ctk_button(parent, text, icon=None, kind='primary', icon_size=20,
              corner_radius=CORNER_RADIUS_SMALL, compound='left', **kwargs):
    """A rounded button in one of the palette's meaningful colours."""
    fg_color, hover_color = _BUTTON_KINDS[kind]
    text_color = kwargs.pop('text_color', readable_on(fg_color))
    image = icon_image(icon, text_color, icon_size) if icon else None
    return ctk.CTkButton(parent, text=text, image=image, compound=compound,
                         fg_color=fg_color, hover_color=hover_color,
                         text_color=text_color, corner_radius=corner_radius,
                         font=FONT_BODY_BOLD, **kwargs)


# --------------------------------------------------------------------------
# Status banner
# --------------------------------------------------------------------------
#
# The one line on a machine page that says what the equipment is doing, and
# the first thing an operator looks at when something stops. As a bare
# coloured word on the page background it was easy to miss from arm's
# length; filled, iconed and set in bold it reads across the cell.
#
# Severity is passed as the colour word the pages already use, so existing
# calls keep working - "red" means a fault, not merely red text.

_LEVELS = {
    'danger':  (DANGER,  DANGER_SOFT,  'alert'),
    'warning': (WARNING, ROW_BAND,     'alert'),
    'success': (SUCCESS, SUCCESS_SOFT, 'check'),
    'info':    (ACCENT,  ACCENT_SOFT,  'info'),
    'idle':    (TEXT_MUTED, SUBTLE,    'info'),
}

_LEVEL_WORDS = {
    'red': 'danger', 'darkred': 'danger', DANGER: 'danger',
    'orange': 'warning', 'yellow': 'warning', WARNING: 'warning',
    'green': 'success', SUCCESS: 'success',
    'blue': 'info', 'navy': 'info', ACCENT: 'info',
    'black': 'idle', 'gray': 'idle', 'grey': 'idle', TEXT_MUTED: 'idle',
}


def level_for(color):
    """The severity a page means by the colour word it passed."""
    if not isinstance(color, str):
        return 'idle'
    value = color.strip().lower()
    if value in _LEVELS:
        return value
    return _LEVEL_WORDS.get(value, 'info')


class StatusBanner(ctk.CTkFrame):
    """A filled strip carrying the current machine message.

    `show(message, colour)` takes the same colour words the pages already
    pass to their message label, so it is a drop-in for one.
    """

    def __init__(self, parent, **kwargs):
        kwargs.setdefault('corner_radius', CORNER_RADIUS_SMALL)
        kwargs.setdefault('fg_color', SUBTLE)
        kwargs.setdefault('height', 34)
        super().__init__(parent, **kwargs)
        self.pack_propagate(False)

        self._icon = ctk.CTkLabel(self, text='', width=18, fg_color='transparent')
        self._icon.pack(side='left', padx=(PAD_LARGE, 0))

        self._text = ctk.CTkLabel(self, text='', fg_color='transparent',
                                  font=FONT_BODY_BOLD, anchor='w')
        self._text.pack(side='left', fill='x', expand=True, padx=PAD)

        self.show('Initializing...', 'idle')

    def show(self, message, color='info'):
        level = level_for(color)
        accent, fill, icon = _LEVELS[level]
        self.configure(fg_color=fill)
        self._icon.configure(image=icon_image(icon, accent, 16))
        self._text.configure(text=message, text_color=accent)

    # A page that still treats this as its old tk.Label keeps working.
    def config(self, cnf=None, **kw):
        if cnf:
            kw = dict(cnf, **kw)
        text = kw.pop('text', None)
        color = kw.pop('fg', kw.pop('foreground', None))
        if text is not None or color is not None:
            self.show(text if text is not None else self._text.cget('text'),
                      color or 'info')
        if kw:
            super().configure(**kw)

    configure = config


# --------------------------------------------------------------------------
# Step lamp
# --------------------------------------------------------------------------
#
# One stage of the test cycle: AUTO, HOME, the two pulls, the result. These
# are indicators, not controls - the monitoring loop recolours them as each
# stage passes or fails, and nothing happens if you click one.
#
# They used to be flat square tk.Labels with a hand cursor, which in an app
# where every real button is a rounded pill made a row of five of them read
# as a button bar. The colour now lives on a rounded frame, and the icon and
# text are redrawn in whatever reads against it.
#
# The monitoring loop addresses these as `lamp.config(bg=...)` and reads the
# colour back with `cget('bg')`, so both are kept working rather than
# rewritten across the dozen call sites that use them.

class StepLamp(ctk.CTkFrame):

    def __init__(self, parent, text, icon, color=ACCENT_FILL, icon_size=22,
                 **kwargs):
        kwargs.setdefault('corner_radius', CORNER_RADIUS_SMALL)
        super().__init__(parent, fg_color=color, **kwargs)

        self._icon_name = icon
        self._icon_size = icon_size
        self._color = color

        self._label = ctk.CTkLabel(self, text=text, compound='top',
                                   fg_color='transparent',
                                   font=FONT_BODY_BOLD)
        self._label.pack(expand=True, fill='both', padx=PAD, pady=PAD)
        self.set_color(color)

    def set_color(self, color):
        """Light this lamp in `color`, keeping icon and text legible on it."""
        color = map_background(color)
        ink = _FILL_TEXT.get(color) or readable_on(color)
        self._color = color
        self.configure(fg_color=color)
        self._label.configure(text_color=ink,
                              image=icon_image(self._icon_name, ink,
                                               self._icon_size))

    # -- the tk.Label surface the monitoring loop still speaks -------------

    def config(self, cnf=None, **kw):
        if cnf:
            kw = dict(cnf, **kw)
        background = kw.pop('bg', kw.pop('background', None))
        # fg is derived from the background, so a caller setting one is
        # telling us something we have already worked out.
        kw.pop('fg', None)
        kw.pop('foreground', None)
        kw.pop('cursor', None)
        if background is not None:
            self.set_color(background)
        if kw:
            super().configure(**kw)

    configure = config

    def cget(self, key):
        if key in ('bg', 'background'):
            return self._color
        return super().cget(key)
