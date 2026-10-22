"""
vision_settings.py
===================
Vision Settings page.

Teach, tune and verify the template-matching models that gate production.

The page is a thin shell over the vision engine (vision_engine/part_inspection and
vision_engine/stripes): it never matches or judges anything itself, so what an operator verifies here is exactly what the
test cycle will do on the line.

It is built like the other consoles - handed a window or a panel to fill - and
drawn in the shared palette from ui.py.
"""
import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import customtkinter as ctk

import ui

try:
    import cv2
    import numpy as np
    from vision_engine.enhance import enhance
    _cv2_ok = True
except ImportError:
    _cv2_ok = False

try:
    from PIL import Image, ImageTk
    _pil_ok = True
except ImportError:
    _pil_ok = False


# ── Palette ────────────────────────────────────────────────────────────────────
# Taken from ui.py so the page reads as part of the same application.
BG          = ui.APP_BG
PANEL       = ui.SURFACE
LINE        = ui.BORDER
FIELD       = ui.SURFACE
VIEW_BG     = ui.SUBTLE          # behind an image, where it does not fill the view
TXT         = ui.TEXT
TXT_DIM     = ui.TEXT_MUTED
TXT_FAINT   = ui.TEXT_MUTED
ACCENT      = ui.ACCENT
OK_GREEN    = ui.SUCCESS
NG_RED      = ui.DANGER
WARN        = ui.WARNING_HOVER   # the darker orange, to stay legible on white

FONT = ui.FONT_FAMILY
MONO = "Consolas"

BTN_PRIMARY = "primary"
BTN_SUCCESS = "success"
BTN_DANGER  = "danger"
BTN_NEUTRAL = "neutral"

_BUTTON_COLOURS = {
    BTN_PRIMARY: (ui.ACCENT_FILL, ui.ACCENT_HOVER),
    BTN_SUCCESS: (ui.SUCCESS, ui.SUCCESS_HOVER),
    BTN_DANGER:  (ui.DANGER, ui.DANGER_HOVER),
    BTN_NEUTRAL: (ui.SUBTLE, ui.BORDER),
}

_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vision_models")


# ── Camera config (same file and keys the vision engine reads) ─────────────────

def _camera_source() -> str:
    from vision_engine import load_vision_config
    return load_vision_config().get("camera_source", "cam1")


def _load_cam_cfg(source=None) -> dict:
    """Saved device for a camera source; the inspection camera by default."""
    from vision_engine import load_camera_config
    index, width, height = load_camera_config(source or _camera_source())
    return {"index": index, "width": width, "height": height, "enabled": index >= 0}


def _save_cam_cfg(index: int, width: int, height: int, enabled: bool, source=None):
    """Store a camera; a disabled camera is saved as index -1."""
    from vision_engine import save_camera_config
    save_camera_config(source or _camera_source(), index if enabled else -1, width, height)


def _probe_cameras(max_index: int = 6):
    """Indices that open *and* deliver a frame, with the sizes they deliver.

    A device that opens but never yields a frame is worse than no device at all —
    it looks configured and then fails mid-cycle — so opening is not enough to
    call a camera present. Each has "sizes", smallest first, and "width" and
    "height", its largest.
    """
    found = []
    if not _cv2_ok:
        return found
    from vision_engine import camera
    for i in range(max_index):
        try:
            cam = camera.probe(i)
        except Exception:
            cam = None
        if cam:
            cam["width"], cam["height"] = max(cam["sizes"], key=lambda s: s[0] * s[1])
            found.append(cam)
    return found


def _cam_label(cam: dict) -> str:
    """How a found camera reads in the list of devices."""
    return "Camera %d  (up to %dx%d)" % (cam["index"], cam["width"], cam["height"])


def _read_image(path):
    """cv2.imread that also opens paths with non-ASCII characters on Windows."""
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


# ── Part master (the model master is where every part number is defined) ───────

def _fetch_master_parts():
    """[(part number, model name), ...] from the model master, or None if it
    can't be reached.

    The page otherwise only talks to the camera and the filesystem, so this
    stays a soft, best-effort lookup: callers fall back to free-text entry
    when it returns None instead of blocking teaching.
    """
    try:
        import db
        conn = db.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT MM_PART_NUMBER, MM_MODEL_NAME FROM TBL_MODEL_MASTER "
                        "ORDER BY MM_PART_NUMBER")
            rows = [(str(r[0]), r[1] or "") for r in cur.fetchall() if r[0]]
            cur.close()
        finally:
            conn.close()
        return rows
    except Exception:
        return None


def _part_choice_label(pno, pname):
    return "%s — %s" % (pno, pname) if pname else pno


class _PnoField:
    """Part-number input: a locked-to-the-master combobox when the model master
    is reachable, a free-text Entry when it isn't. Exposes the same get / set /
    lock / focus_set / bind / pack surface either way so call sites don't need
    to know which one is live underneath.
    """

    def __init__(self, parent, master_parts, font_size=15):
        self._by_label = {}
        self.is_master_backed = bool(master_parts)
        if master_parts:
            labels = []
            for pno, pname in master_parts:
                label = _part_choice_label(pno, pname)
                self._by_label[label] = pno
                labels.append(label)
            self.widget = ttk.Combobox(parent, state="readonly", values=labels,
                                       font=(MONO, font_size))
        else:
            self.widget = tk.Entry(parent, bg=FIELD, fg=TXT,
                                   font=(MONO, font_size),
                                   insertbackground=TXT, relief="flat",
                                   highlightthickness=1, highlightbackground=LINE,
                                   highlightcolor=ACCENT)

    def get(self) -> str:
        raw = self.widget.get().strip()
        if raw in self._by_label:
            return self._by_label[raw]
        return raw.upper()

    def set(self, pno):
        if self.is_master_backed:
            label = next((l for l, p in self._by_label.items() if p == pno), pno)
            self.widget.set(label)
        else:
            self.widget.delete(0, "end")
            self.widget.insert(0, pno)

    def lock(self):
        if self.is_master_backed:
            self.widget.config(state="disabled")
        else:
            self.widget.config(state="readonly", readonlybackground=FIELD, fg=TXT_DIM)

    def focus_set(self):
        self.widget.focus_set()

    def pack(self, **kw):
        self.widget.pack(**kw)

    def bind(self, sequence, func):
        self.widget.bind(sequence, func)
        # A readonly combobox never emits KeyRelease from a pick — it emits
        # its own selection event — so route both through the same handler.
        if self.is_master_backed and sequence == "<KeyRelease>":
            self.widget.bind("<<ComboboxSelected>>", func)


# ── Small widget helpers ───────────────────────────────────────────────────────

def _btn(parent, text, kind=BTN_NEUTRAL, command=None, width=None, font_size=11,
         pady=6, icon=None):
    """A rounded button in one of the palette's meaningful colours."""
    fg, hover = _BUTTON_COLOURS[kind]
    ink = ui.readable_on(fg)
    b = ctk.CTkButton(parent, text=text, command=command,
                      fg_color=fg, hover_color=hover, text_color=ink,
                      text_color_disabled=ui.DISABLED_TEXT,
                      corner_radius=ui.CORNER_RADIUS_SMALL,
                      font=(FONT, font_size, "bold"),
                      width=width or 60, height=font_size * 2 + pady * 2,
                      image=ui.icon_image(icon, ink, 18) if icon else None,
                      compound="left")
    b._colors = (fg, hover)
    b._icon = (icon, ink)
    return b


def _set_btn_enabled(btn, enabled: bool):
    icon, ink = btn._icon
    if enabled:
        btn.configure(state="normal", fg_color=btn._colors[0])
    else:
        btn.configure(state="disabled", fg_color=ui.DISABLED_BG)
    if icon:
        btn.configure(image=ui.icon_image(icon, ink if enabled else ui.DISABLED_TEXT, 18))


def _card(parent, title, subtitle=None, icon=None):
    """Rounded card with the navy title strip. Returns the card; fill `.body`."""
    outer = ui.ctk_card(parent)
    head = ui.ctk_card_header(outer, title.upper(), icon=icon)
    if subtitle:
        ctk.CTkLabel(head, text=subtitle, fg_color=ui.NAVY,
                     text_color=ui.ACCENT_SOFT, font=(FONT, 10)).pack(
                         side="left", padx=(4, 0))

    body = tk.Frame(outer, bg=PANEL)
    body.pack(fill="both", expand=True, padx=14, pady=12)
    outer.head = head
    outer.body = body
    return outer


def _kv_row(parent, label, value="—", value_fg=TXT, mono=False):
    """One label/value line. Returns the value label so callers can update it."""
    f = tk.Frame(parent, bg=parent["bg"])
    f.pack(fill="x", pady=2)
    tk.Label(f, text=label, bg=parent["bg"], fg=TXT_DIM, font=(FONT, 11),
             width=11, anchor="w").pack(side="left")
    v = tk.Label(f, text=value, bg=parent["bg"], fg=value_fg, anchor="w",
                 font=(MONO, 11) if mono else (FONT, 10, "bold"))
    v.pack(side="left", fill="x", expand=True)
    return v


def _to_photo(img_bgr, box_w, box_h):
    """BGR ndarray → PhotoImage scaled to fit (box_w, box_h). Returns (photo, scale)."""
    if img_bgr.ndim == 2:
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_GRAY2RGB)
    else:
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    ih, iw = rgb.shape[:2]
    scale = min(box_w / iw, box_h / ih)
    dw, dh = max(1, int(iw * scale)), max(1, int(ih * scale))
    im = Image.fromarray(rgb).resize((dw, dh), Image.Resampling.BILINEAR)
    return ImageTk.PhotoImage(im), scale


def _threshold_caption(value: float) -> tuple:
    """Plain-language reading of a correlation threshold."""
    if value < 0.55:
        return "Very lenient — almost any frame will pass", NG_RED
    if value < 0.68:
        return "Lenient — tolerates lighting and position drift", WARN
    if value < 0.85:
        return "Balanced — recommended for production", OK_GREEN
    if value < 0.94:
        return "Strict — needs consistent lighting and fixturing", WARN
    return "Very strict — near-identical frames only", NG_RED


# ═══════════════════════════════════════════════════════════════════════════════
# ROI marker
# ═══════════════════════════════════════════════════════════════════════════════

class RoiView(tk.Canvas):
    """Image view with a draggable, resizable region-of-interest marker.

    The marker lives as canvas items on top of the image instead of being burnt
    into the pixel buffer, so it stays sharp at any display scale and can be
    nudged or resized after it is drawn.

    Interaction:
      drag on empty space  -> draw a new box
      drag inside the box  -> move it
      drag a handle        -> resize from that edge or corner
      Delete / Escape      -> clear

    With a locked size (see lock_size) the handles disappear and a click drops a
    box of that exact size where you point, so a box drawn on one image can be
    repositioned onto the part in the next without changing what it crops.

    With `enhanced`, the picture is shown with its light evened out, as the
    vision checks see it when teaching; get_image() still returns it as given.
    """

    HANDLE = 4          # handle half-size, screen px
    GRAB = 7            # grab tolerance around an edge, screen px
    MIN_ROI = 10        # smallest useful template, image px

    _CURSORS = {
        "nw": "size_nw_se", "se": "size_nw_se",
        "ne": "size_ne_sw", "sw": "size_ne_sw",
        "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
        "w": "sb_h_double_arrow", "e": "sb_h_double_arrow",
        "move": "fleur",
    }

    MAX_ZOOM = 16.0
    ZOOM_STEP = 1.25

    def __init__(self, parent, on_change=None, editable=True, zoomable=False,
                 on_zoom=None, enhanced=False, **kw):
        super().__init__(parent, bg=VIEW_BG, highlightthickness=0,
                         bd=0, cursor="crosshair", **kw)
        self._on_change = on_change
        self._editable = editable
        self._image = None          # BGR ndarray
        self._shown = None          # what is drawn: _image, enhanced or not
        self._enhanced = enhanced
        self._roi = None            # {"x","y","width","height"} in image coords
        self._view = None           # (scale, ox, oy, dw, dh)
        self._photo = None
        self._drag = None
        self._placeholder = "No image"
        self._hint = None
        self._accent = OK_GREEN
        self._locked_size = None    # (w, h) in image px, or None for free drawing
        # Shapes drawn over the image, as point lists in image coordinates
        self._outlines = []
        self._outline_color = ui.ACCENT_FILL
        # Labelled boxes that are shown but not edited: (roi, colour, label)
        self._frames = []
        # Zoom: 1 fits the whole image. `_centre` is the image point shown at
        # the middle of the view, or None for the middle of the image.
        self._zoomable = zoomable
        self._on_zoom = on_zoom
        self._zoom = 1.0
        self._centre = None
        self._pan = None

        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Motion>", self._on_hover)
        self.bind("<Leave>", lambda e: self.config(cursor="crosshair"))
        self.bind("<Delete>", self.clear_roi)
        self.bind("<Escape>", self.clear_roi)
        if zoomable:
            # Wheel zooms; the right or middle button drags the picture, so
            # the left button stays free for drawing the box
            self.bind("<MouseWheel>", self._on_wheel)
            for button in (2, 3):
                self.bind("<ButtonPress-%d>" % button, self._on_pan_start)
                self.bind("<B%d-Motion>" % button, self._on_pan)
                self.bind("<ButtonRelease-%d>" % button, self._on_pan_end)

    # -- public API ---------------------------------------------------------

    def set_image(self, img, keep_roi=True):
        prev_shape = None if self._image is None else self._image.shape[:2]
        self._image = img
        self._shown = enhance(img) if img is not None and self._enhanced else img
        if img is not None:
            if not keep_roi:
                self._roi = None
            elif prev_shape and prev_shape != img.shape[:2]:
                self._roi = None        # a box means nothing at a new resolution
            if prev_shape != img.shape[:2]:
                # The same zoom on a frame of the same size, so a live view
                # stays where it was zoomed to
                self._set_zoom(1.0, None)
        self._redraw()

    # -- zoom ---------------------------------------------------------------

    def get_zoom(self):
        return self._zoom

    def zoom_by(self, factor):
        """Zoom about the middle of the view."""
        if self._view is None:
            return
        self._zoom_at(factor, self.winfo_width() / 2, self.winfo_height() / 2)

    def zoom_fit(self):
        self._set_zoom(1.0, None)
        self._redraw()

    def _set_zoom(self, zoom, centre):
        zoom = min(max(zoom, 1.0), self.MAX_ZOOM)
        changed = abs(zoom - self._zoom) > 1e-6
        self._zoom = zoom
        self._centre = centre if zoom > 1.0 else None
        if changed and self._on_zoom:
            self._on_zoom(zoom)

    def _zoom_at(self, factor, sx, sy):
        """Zoom by `factor`, keeping the image point under (sx, sy) in place."""
        s, ox, oy, _, _ = self._view
        ix, iy = (sx - ox) / s, (sy - oy) / s
        new = min(max(self._zoom * factor, 1.0), self.MAX_ZOOM)
        s2 = s * new / self._zoom
        # Put the centre where it keeps (ix, iy) under the pointer
        cx = (self.winfo_width() / 2 - (sx - ix * s2)) / s2
        cy = (self.winfo_height() / 2 - (sy - iy * s2)) / s2
        self._set_zoom(new, (cx, cy))
        self._redraw()

    def _on_wheel(self, event):
        if self._view is not None and self._drag is None:
            self._zoom_at(self.ZOOM_STEP if event.delta > 0 else 1 / self.ZOOM_STEP,
                          event.x, event.y)
        return "break"      # the page's scroll area must not scroll as well

    def _on_pan_start(self, event):
        if self._view is None or self._zoom <= 1.0:
            return
        s, ox, oy, _, _ = self._view
        self._pan = (event.x, event.y, ((self.winfo_width() / 2 - ox) / s,
                                        (self.winfo_height() / 2 - oy) / s))
        self.config(cursor="fleur")

    def _on_pan(self, event):
        if self._pan is None or self._view is None:
            return
        x0, y0, (cx, cy) = self._pan
        s = self._view[0]
        self._centre = (cx - (event.x - x0) / s, cy - (event.y - y0) / s)
        self._redraw()

    def _on_pan_end(self, event):
        self._pan = None
        self.config(cursor="crosshair" if self._editable else "arrow")

    def get_image(self):
        return self._image

    def set_placeholder(self, text):
        self._placeholder = text
        if self._image is None:
            self._redraw()

    def set_hint(self, text):
        """Caption drawn along the bottom of the view."""
        self._hint = text
        self._redraw()

    def set_editable(self, editable):
        self._editable = editable
        self.config(cursor="crosshair" if editable else "arrow")
        self._redraw()

    def set_accent(self, color):
        self._accent = color
        self._redraw()

    def set_outlines(self, outlines, color=None):
        """Draw these shapes over the image, as lines that stay thin at any zoom."""
        self._outlines = [list(map(tuple, o)) for o in (outlines or [])]
        if color:
            self._outline_color = color
        self._redraw()

    def set_frames(self, frames):
        """Draw these boxes over the image, dashed and labelled, without
        making them editable. `frames` lists (roi, colour, label)."""
        self._frames = [(dict(r), c, t) for r, c, t in (frames or []) if r]
        self._redraw()

    def lock_size(self, wh):
        """Pin the box to (w, h), or pass None to allow free drawing again."""
        self._locked_size = tuple(wh) if wh else None
        self._redraw()

    def _place_locked(self, cx, cy):
        """A locked-size box centred on (cx, cy), kept inside the image."""
        w, h = self._locked_size
        ih, iw = self._image.shape[:2]
        w, h = min(w, iw), min(h, ih)
        x = min(max(cx - w // 2, 0), iw - w)
        y = min(max(cy - h // 2, 0), ih - h)
        return {"x": int(x), "y": int(y), "width": int(w), "height": int(h)}

    def get_roi(self):
        return dict(self._roi) if self._roi else None

    def set_roi(self, roi, notify=True):
        self._roi = dict(roi) if roi else None
        self._redraw()
        if notify:
            self._notify()

    def clear_roi(self, event=None):
        if not self._editable:
            return
        self._roi = None
        self._redraw()
        self._notify()

    # -- coordinate mapping -------------------------------------------------

    def _compute_view(self):
        if self._image is None:
            self._view = None
            return
        cw, ch = self.winfo_width(), self.winfo_height()
        if cw < 5 or ch < 5:
            self._view = None
            return
        ih, iw = self._image.shape[:2]
        scale = min(cw / iw, ch / ih) * self._zoom
        dw, dh = max(1, int(iw * scale)), max(1, int(ih * scale))
        if self._centre is None:
            ox, oy = (cw - dw) // 2, (ch - dh) // 2
        else:
            # Centre on the chosen point, but never scroll past an edge of
            # the image while it is bigger than the view
            ox = cw / 2 - self._centre[0] * scale
            oy = ch / 2 - self._centre[1] * scale
            ox = (cw - dw) / 2 if dw <= cw else min(0, max(cw - dw, ox))
            oy = (ch - dh) / 2 if dh <= ch else min(0, max(ch - dh, oy))
            self._centre = ((cw / 2 - ox) / scale, (ch / 2 - oy) / scale)
        self._view = (scale, ox, oy, dw, dh)

    def _to_screen(self, ix, iy):
        s, ox, oy, _, _ = self._view
        return ox + ix * s, oy + iy * s

    def _to_image(self, sx, sy):
        s, ox, oy, _, _ = self._view
        ih, iw = self._image.shape[:2]
        ix = min(max((sx - ox) / s, 0), iw)
        iy = min(max((sy - oy) / s, 0), ih)
        return int(round(ix)), int(round(iy))

    def _roi_screen(self):
        r = self._roi
        x0, y0 = self._to_screen(r["x"], r["y"])
        x1, y1 = self._to_screen(r["x"] + r["width"], r["y"] + r["height"])
        return x0, y0, x1, y1

    # -- drawing ------------------------------------------------------------

    def _redraw(self):
        self.delete("all")
        self._compute_view()
        cw, ch = max(self.winfo_width(), 1), max(self.winfo_height(), 1)

        if self._view is None:
            self.create_text(cw // 2, ch // 2, text=self._placeholder,
                             fill=TXT_FAINT, font=(FONT, 13), justify="center")
            return

        s, ox, oy, dw, dh = self._view
        if self._zoom <= 1.0:
            self._photo, _ = _to_photo(self._shown, dw, dh)
            self.create_image(ox, oy, image=self._photo, anchor="nw")
        else:
            self._draw_visible(s, ox, oy, cw, ch)
        self.create_rectangle(ox, oy, ox + dw, oy + dh, outline=LINE)

        for shape in self._outlines:
            pts = [c for x, y in shape for c in self._to_screen(x, y)]
            self.create_polygon(*pts, outline=self._outline_color, fill="", width=2)

        for r, colour, label in self._frames:
            x0, y0 = self._to_screen(r["x"], r["y"])
            x1, y1 = self._to_screen(r["x"] + r["width"], r["y"] + r["height"])
            self.create_rectangle(x0, y0, x1, y1, outline=colour, width=2, dash=(6, 3))
            if label:
                tid = self.create_text(x0 + 3, y0 + 3, text=label, anchor="nw",
                                       fill=ui.readable_on(colour), font=(MONO, 10, "bold"))
                bx0, by0, bx1, by1 = self.bbox(tid)
                rid = self.create_rectangle(bx0 - 3, by0 - 1, bx1 + 3, by1 + 1,
                                            fill=colour, outline="")
                self.tag_raise(tid, rid)

        if self._roi:
            self._draw_marker()
        elif self._drag and self._drag["mode"] == "new":
            x0, y0 = self._to_screen(*self._drag["anchor"])
            x1, y1 = self._to_screen(*self._drag["cursor"])
            self.create_rectangle(x0, y0, x1, y1, outline=ui.ACCENT_FILL, width=2,
                                  dash=(4, 3))

        if self._hint:
            tid = self.create_text(cw // 2, ch - 12, text=self._hint, fill=TXT,
                                   font=(FONT, 11))
            bx0, by0, bx1, by1 = self.bbox(tid)
            rid = self.create_rectangle(bx0 - 6, by0 - 2, bx1 + 6, by1 + 2,
                                        fill=PANEL, outline=LINE)
            self.tag_raise(tid, rid)

    def _draw_visible(self, s, ox, oy, cw, ch):
        """Draw just the part of the image the zoomed view shows.

        Scaling the whole frame at 16x would build an image hundreds of
        megapixels big on every redraw.
        """
        ih, iw = self._image.shape[:2]
        x0 = max(0, int(-ox / s))
        y0 = max(0, int(-oy / s))
        x1 = min(iw, int((cw - ox) / s) + 2)
        y1 = min(ih, int((ch - oy) / s) + 2)
        if x1 <= x0 or y1 <= y0:
            return
        patch = self._shown[y0:y1, x0:x1]
        rgb = cv2.cvtColor(patch, cv2.COLOR_GRAY2RGB if patch.ndim == 2 else cv2.COLOR_BGR2RGB)
        w, h = max(1, round((x1 - x0) * s)), max(1, round((y1 - y0) * s))
        # Close in, show the real pixels rather than a blur, so a stripe's
        # edge is where the camera saw it
        resample = Image.Resampling.NEAREST if s >= 2 else Image.Resampling.BILINEAR
        self._photo = ImageTk.PhotoImage(Image.fromarray(rgb).resize((w, h), resample))
        self.create_image(ox + x0 * s, oy + y0 * s, image=self._photo, anchor="nw")

    def _draw_marker(self):
        s, ox, oy, dw, dh = self._view
        x0, y0, x1, y1 = self._roi_screen()

        # Dim everything outside the ROI so the taught region reads at a glance.
        for box in ((ox, oy, ox + dw, y0), (ox, y1, ox + dw, oy + dh),
                    (ox, y0, x0, y1), (x1, y0, ox + dw, y1)):
            if box[2] > box[0] and box[3] > box[1]:
                self.create_rectangle(*box, fill="#000000", outline="",
                                      stipple="gray50")

        self.create_rectangle(x0, y0, x1, y1, outline=self._accent, width=2)

        # Corner arms read as a machine-vision reticle rather than a plain box.
        arm = min(18, max(6, int((x1 - x0) // 4)), max(6, int((y1 - y0) // 4)))
        for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                               (x0, y1, 1, -1), (x1, y1, -1, -1)):
            self.create_line(cx, cy, cx + arm * sx, cy, fill=self._accent, width=4)
            self.create_line(cx, cy, cx, cy + arm * sy, fill=self._accent, width=4)

        if self._editable and self._locked_size is None:
            for _, hx, hy in self._handles(x0, y0, x1, y1):
                self.create_rectangle(hx - self.HANDLE, hy - self.HANDLE,
                                      hx + self.HANDLE, hy + self.HANDLE,
                                      fill=self._accent, outline=PANEL)

        label = "%d x %d px" % (self._roi["width"], self._roi["height"])
        ly = y0 - 11 if y0 - 11 > oy + 8 else y1 + 12
        tid = self.create_text(x0 + 2, ly, text=label,
                               fill=ui.readable_on(self._accent), anchor="w",
                               font=(MONO, 11, "bold"))
        bx0, by0, bx1, by1 = self.bbox(tid)
        rid = self.create_rectangle(bx0 - 4, by0 - 2, bx1 + 4, by1 + 2,
                                    fill=self._accent, outline="")
        self.tag_raise(tid, rid)

    @staticmethod
    def _handles(x0, y0, x1, y1):
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        return (("nw", x0, y0), ("n", mx, y0), ("ne", x1, y0),
                ("w", x0, my), ("e", x1, my),
                ("sw", x0, y1), ("s", mx, y1), ("se", x1, y1))

    # -- hit testing --------------------------------------------------------

    def _hit(self, sx, sy):
        """'nw'..'se' for a handle, 'move' inside the box, or None."""
        if not self._roi or self._view is None:
            return None
        x0, y0, x1, y1 = self._roi_screen()
        if self._locked_size is None:
            for name, hx, hy in self._handles(x0, y0, x1, y1):
                if abs(sx - hx) <= self.GRAB and abs(sy - hy) <= self.GRAB:
                    return name
        if x0 <= sx <= x1 and y0 <= sy <= y1:
            return "move"
        return None

    def _on_hover(self, event):
        if not self._editable or self._drag:
            return
        self.config(cursor=self._CURSORS.get(self._hit(event.x, event.y), "crosshair"))

    # -- interaction --------------------------------------------------------

    def _on_press(self, event):
        if not self._editable or self._view is None:
            return
        self.focus_set()
        hit = self._hit(event.x, event.y)
        if hit:
            self._drag = {"mode": hit, "roi0": dict(self._roi),
                          "origin": self._to_image(event.x, event.y)}
        elif self._locked_size is not None:
            # Locked: drop the box where they pointed and let the same gesture
            # nudge it, rather than making them draw a box that cannot resize.
            pt = self._to_image(event.x, event.y)
            self._roi = self._place_locked(*pt)
            self._drag = {"mode": "move", "roi0": dict(self._roi), "origin": pt}
            self._redraw()
            self._notify()
        else:
            anchor = self._to_image(event.x, event.y)
            self._drag = {"mode": "new", "anchor": anchor, "cursor": anchor}
            self._roi = None
            self._redraw()

    def _on_drag(self, event):
        if not self._drag:
            return
        pt = self._to_image(event.x, event.y)
        mode = self._drag["mode"]

        if mode == "new":
            self._drag["cursor"] = pt
        elif mode == "move":
            r0 = self._drag["roi0"]
            ox_, oy_ = self._drag["origin"]
            ih, iw = self._image.shape[:2]
            nx = min(max(r0["x"] + pt[0] - ox_, 0), iw - r0["width"])
            ny = min(max(r0["y"] + pt[1] - oy_, 0), ih - r0["height"])
            self._roi = {"x": nx, "y": ny,
                         "width": r0["width"], "height": r0["height"]}
        else:
            self._roi = self._resized(self._drag["roi0"], mode, pt)

        self._redraw()
        if mode != "new":
            self._notify(final=False)

    def _resized(self, r0, mode, pt):
        left, top = r0["x"], r0["y"]
        right, bottom = r0["x"] + r0["width"], r0["y"] + r0["height"]
        px, py = pt
        if "n" in mode:
            top = min(py, bottom - self.MIN_ROI)
        if "s" in mode:
            bottom = max(py, top + self.MIN_ROI)
        if "w" in mode:
            left = min(px, right - self.MIN_ROI)
        if "e" in mode:
            right = max(px, left + self.MIN_ROI)
        return {"x": int(left), "y": int(top),
                "width": int(right - left), "height": int(bottom - top)}

    def _on_release(self, event):
        if not self._drag:
            return
        if self._drag["mode"] == "new":
            (ax, ay), (bx, by) = self._drag["anchor"], self._drag["cursor"]
            x, y = min(ax, bx), min(ay, by)
            w, h = abs(bx - ax), abs(by - ay)
            self._roi = ({"x": x, "y": y, "width": w, "height": h}
                         if w >= self.MIN_ROI and h >= self.MIN_ROI else None)
        self._drag = None
        self._redraw()
        self._notify()

    def _notify(self, final=True):
        """`final` is False for the intermediate states of a drag, so listeners
        can keep cheap readouts live but defer expensive redraws to the release."""
        if self._on_change:
            self._on_change(self.get_roi(), final)


# ═══════════════════════════════════════════════════════════════════════════════
# Page
# ═══════════════════════════════════════════════════════════════════════════════

_ORPHAN = "!unmapped:"


class VisionSettings:
    """The page as a console: title bar, footer, and the page between them."""

    def __init__(self, root):
        self.root = root
        ui.apply(root)
        self.root.title("EOL Tester - Vision Settings")

        # The footer is packed before the body so it keeps the foot of the window.
        ui.page_header(root, "Vision Settings")
        ui.footer_bar(root)
        render(root)


def render(parent):
    """Render the Vision Settings page."""
    from vision_engine import VisionController, load_vision_config, save_vision_config
    from vision_engine.config import DEFAULT_MATCH_THRESHOLD

    v_cfg = load_vision_config()
    ctrl = VisionController()
    alive = {"page": True}

    content = tk.Frame(parent, bg=BG)
    content.pack(fill="both", expand=True, padx=18, pady=14)
    content.columnconfigure(0, weight=1)
    content.rowconfigure(2, weight=1)

    # ── Header ─────────────────────────────────────────────────────────────
    header = tk.Frame(content, bg=BG)
    header.grid(row=0, column=0, sticky="ew", pady=(0, 12))

    tk.Label(header, text="Part-presence verification by template matching "
                          "(normalised cross-correlation)",
             bg=BG, fg=TXT_DIM, font=(FONT, 11)).pack(side="left")

    pill = tk.Frame(header, bg=ui.SUBTLE, padx=12, pady=7,
                    highlightthickness=1, highlightbackground=LINE)
    pill.pack(side="right")
    pill_dot = tk.Label(pill, text="●", bg=ui.SUBTLE, fg=TXT_FAINT, font=(FONT, 14))
    pill_dot.pack(side="left", padx=(0, 7))
    pill_txt = tk.Label(pill, text="Checking camera…", bg=ui.SUBTLE, fg=TXT_DIM,
                        font=(FONT, 11, "bold"))
    pill_txt.pack(side="left")

    if not _cv2_ok or not _pil_ok:
        missing = " and ".join(n for n, ok in
                               (("opencv-python", _cv2_ok), ("Pillow", _pil_ok)) if not ok)
        bar = tk.Frame(content, bg=ui.ROW_BAND)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        tk.Label(bar, text="  %s is not installed — vision is unavailable until it is."
                           % missing, bg=ui.ROW_BAND, fg=TXT,
                 font=(FONT, 11, "bold"), pady=6).pack(anchor="w")

    # ── Body: parts table + right rail ─────────────────────────────────────
    body = tk.Frame(content, bg=BG)
    body.grid(row=2, column=0, sticky="nsew")
    body.columnconfigure(0, weight=1)
    body.rowconfigure(0, weight=1)

    parts_card = _card(body, "Taught Parts",
                       "each part number maps to one template dataset", icon="list")
    parts_card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

    rail_scroll = ui.scrollable(body)
    rail_scroll.configure(width=340)
    rail_scroll.grid(row=0, column=1, sticky="ns")
    rail_scroll.grid_propagate(False)
    rail = rail_scroll.body

    # ── Parts table ────────────────────────────────────────────────────────
    pb = parts_card.body
    toolbar = tk.Frame(pb, bg=PANEL)
    toolbar.pack(fill="x", pady=(0, 10))

    table_wrap = tk.Frame(pb, bg=PANEL)
    table_wrap.pack(fill="both", expand=True)

    cols = ("part", "file", "refs", "roi", "thresh", "taught", "status")
    heads = {"part": ("PART NUMBER", 150, "w"), "file": ("MODEL FILE", 150, "w"),
             "refs": ("REFS", 60, "center"), "roi": ("TEMPLATE", 115, "center"),
             "thresh": ("THRESHOLD", 115, "center"),
             "taught": ("TAUGHT", 185, "center"), "status": ("STATUS", 150, "w")}

    tree = ttk.Treeview(table_wrap, columns=cols, show="headings", selectmode="browse")
    sb = ttk.Scrollbar(table_wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tree.pack(fill="both", expand=True)

    for c in cols:
        text, width, anchor = heads[c]
        tree.heading(c, text=text)
        tree.column(c, width=width, anchor=anchor,
                    stretch=(c in ("part", "file", "status")))

    tree.tag_configure("ready", foreground=TXT)
    tree.tag_configure("problem", foreground=WARN)
    tree.tag_configure("empty", foreground=TXT_FAINT)

    # Contextual strip under the table: explains the selected row and carries
    # the one action that only makes sense for unmapped files.
    detail = tk.Frame(pb, bg=PANEL, height=34)
    detail.pack(fill="x", pady=(8, 0))
    detail.pack_propagate(False)
    detail_lbl = tk.Label(detail, text="", bg=PANEL, fg=TXT_FAINT,
                          font=(FONT, 11), anchor="w")
    detail_lbl.pack(side="left", fill="x", expand=True)
    btn_map = _btn(detail, "Map to Part…", BTN_PRIMARY, pady=3, font_size=10)

    def _refresh_table(select=None):
        if not alive["page"]:
            return
        remembered = select or (tree.selection()[0] if tree.selection() else None)
        tree.delete(*tree.get_children())
        ctrl.reload_config()

        mapped_files = set()
        for pno, filename in sorted(ctrl.get_mapped_parts().items()):
            mapped_files.add(filename)
            info = ctrl.model_info(pno)
            if info is None:
                tree.insert("", "end", iid=pno, tags=("problem",),
                            values=(pno, filename, "—", "—", "—", "—", "FILE MISSING"))
            else:
                tw, th = info["template_size"]
                sizes = ("%d x %d" % (tw, th) if info["uniform_templates"]
                         else "varied (%d)" % len(set(info["template_sizes"])))
                # A threshold this low passes almost any frame, so the part looks
                # guarded while nothing is really being checked. Say so.
                weak = info["threshold"] < 0.55
                tree.insert("", "end", iid=pno, tags=("problem" if weak else "ready",),
                            values=(pno, filename, info["references"],
                                    sizes,
                                    "%.2f" % info["threshold"],
                                    str(info["created"]).replace("T", "  "),
                                    "Threshold too low" if weak else "Ready"))

        # Files on disk that no part number resolves to. Production cannot reach
        # these, so surface them rather than letting them look installed.
        if os.path.isdir(_MODELS_DIR):
            for f in sorted(os.listdir(_MODELS_DIR)):
                if f.endswith(".npz") and f not in mapped_files:
                    tree.insert("", "end", iid=_ORPHAN + f, tags=("problem",),
                                values=("—", f, "—", "—", "—", "—", "NOT MAPPED"))

        if not tree.get_children():
            tree.insert("", "end", iid="!none", tags=("empty",),
                        values=("—", "No parts taught yet", "", "", "", "",
                                "Start with “Teach New Part”"))

        if remembered and tree.exists(remembered):
            tree.selection_set(remembered)
            tree.see(remembered)
        _on_select()
        _refresh_coverage()

    def _selection():
        """(kind, value) where kind is 'part', 'orphan' or None."""
        sel = tree.selection()
        if not sel or sel[0] == "!none":
            return None, None
        if sel[0].startswith(_ORPHAN):
            return "orphan", sel[0][len(_ORPHAN):]
        return "part", sel[0]

    # ── Right rail: camera ─────────────────────────────────────────────────
    cam_card = _card(rail, "Camera", icon="camera")
    cam_card.pack(fill="x", padx=(0, 6))
    cb = cam_card.body
    cam_device = _kv_row(cb, "Device", "—", mono=True)
    cam_res = _kv_row(cb, "Resolution", "—", mono=True)
    cam_state = _kv_row(cb, "State", "Checking…", value_fg=TXT_DIM)

    cam_btns = tk.Frame(cb, bg=PANEL)
    cam_btns.pack(fill="x", pady=(10, 0))
    btn_cam_cfg = _btn(cam_btns, "Configure…", BTN_NEUTRAL, icon="gear")
    btn_cam_cfg.pack(side="left")
    btn_cam_check = _btn(cam_btns, "Re-check", BTN_NEUTRAL, icon="refresh")
    btn_cam_check.pack(side="left", padx=(6, 0))

    def _paint_camera(state_text, color, dot_color=None):
        if not alive["page"]:
            return
        cam_state.config(text=state_text, fg=color)
        pill_txt.config(text=state_text, fg=color)
        pill_dot.config(fg=dot_color or color)

    def _refresh_camera():
        cam = _load_cam_cfg()
        idx, w, h = cam["index"], cam["width"], cam["height"]
        configured = cam["enabled"] and idx >= 0
        cam_device.config(text=("Camera %d" % idx) if configured else "Not configured",
                          fg=TXT if configured else TXT_FAINT)
        cam_res.config(text="%d x %d" % (w, h) if configured else "—",
                       fg=TXT if configured else TXT_FAINT)

        if not _cv2_ok:
            _paint_camera("OpenCV missing", NG_RED)
            return
        if not configured:
            _paint_camera("No camera configured", WARN)
            return
        _paint_camera("Checking camera…", TXT_DIM)

        def _work():
            try:
                status = ctrl.get_status()
            except Exception:
                status = "CAMERA_ERROR"
            text, color = {
                "READY": ("Camera %d ready" % idx, OK_GREEN),
                "NO_CAMERA": ("No camera configured", WARN),
            }.get(status, ("Camera %d not responding" % idx, NG_RED))
            try:
                if alive["page"]:
                    parent.after(0, lambda: _paint_camera(text, color))
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()

    btn_cam_check.configure(command=lambda: _refresh_camera())

    def _configure_camera():
        if _open_camera_dialog(parent):
            ctrl.reload_config()
            _refresh_camera()

    btn_cam_cfg.configure(command=_configure_camera)

    # ── Right rail: stripe check (camera 2) ────────────────────────────────
    # Camera 2 checks the painted stripes on the cable. It has its own device
    # and its own taught parts, kept apart from the template datasets above.
    from vision_engine.stripes import stripe_check

    stripe_card = _card(rail, "Stripe Check", "camera 2", icon="camera")
    stripe_card.pack(fill="x", padx=(0, 6), pady=(12, 0))
    sb_ = stripe_card.body
    cam2_device = _kv_row(sb_, "Device", "—", mono=True)

    stripe_tree = ttk.Treeview(sb_, columns=("part", "stripes"), show="headings",
                               selectmode="browse", height=5)
    stripe_tree.heading("part", text="PART NUMBER")
    stripe_tree.heading("stripes", text="STRIPES")
    stripe_tree.column("part", width=150, anchor="w")
    stripe_tree.column("stripes", width=110, anchor="center")
    stripe_tree.tag_configure("empty", foreground=TXT_FAINT)
    stripe_tree.pack(fill="x", pady=(8, 0))

    stripe_btns = tk.Frame(sb_, bg=PANEL)
    stripe_btns.pack(fill="x", pady=(10, 0))
    stripe_btns.columnconfigure((0, 1), weight=1, uniform="stripe")
    btn_s_teach = _btn(stripe_btns, "Teach New", BTN_SUCCESS, icon="box")
    btn_s_teach.grid(row=0, column=0, sticky="ew", padx=(0, 3))
    btn_s_reteach = _btn(stripe_btns, "Re-teach", BTN_NEUTRAL, icon="refresh")
    btn_s_reteach.grid(row=0, column=1, sticky="ew", padx=(3, 0))
    btn_s_test = _btn(stripe_btns, "Run Test", BTN_PRIMARY, icon="play")
    btn_s_test.grid(row=1, column=0, sticky="ew", padx=(0, 3), pady=(6, 0))
    btn_s_del = _btn(stripe_btns, "Delete", BTN_DANGER, icon="alert")
    btn_s_del.grid(row=1, column=1, sticky="ew", padx=(3, 0), pady=(6, 0))
    btn_cam2_cfg = _btn(sb_, "Configure Camera 2…", BTN_NEUTRAL, icon="gear")
    btn_cam2_cfg.pack(fill="x", pady=(6, 0))

    def _stripe_selection():
        sel = stripe_tree.selection()
        return sel[0] if sel and sel[0] != "!none" else None

    def _on_stripe_select(event=None):
        chosen = _stripe_selection() is not None
        for b in (btn_s_reteach, btn_s_test, btn_s_del):
            _set_btn_enabled(b, chosen)

    def _refresh_stripes(select=None):
        if not alive["page"]:
            return
        cam2 = _load_cam_cfg(stripe_check.CAMERA_SOURCE)
        cam2_device.config(
            text=("Camera %d  ·  %dx%d" % (cam2["index"], cam2["width"], cam2["height"])
                  if cam2["enabled"] else "Not configured"),
            fg=TXT if cam2["enabled"] else WARN)

        remembered = select or _stripe_selection()
        stripe_tree.delete(*stripe_tree.get_children())
        for pno in stripe_check.taught_parts():
            model = stripe_check.load_model(pno) or {}
            stripe_tree.insert("", "end", iid=pno, values=(
                pno, stripe_check.describe(model)))
        if not stripe_tree.get_children():
            stripe_tree.insert("", "end", iid="!none", tags=("empty",),
                               values=("No parts taught", ""))
        if remembered and stripe_tree.exists(remembered):
            stripe_tree.selection_set(remembered)
            stripe_tree.see(remembered)
        _on_stripe_select()

    def _stripe_teach(part_number=None):
        saved = _open_stripe_teach(parent, part_number)
        if saved:
            _refresh_stripes(select=saved)

    def _stripe_test():
        pno = _stripe_selection()
        if pno:
            _open_stripe_test(parent, pno)

    def _stripe_delete():
        pno = _stripe_selection()
        if not pno or not messagebox.askyesno(
                "Delete Stripe Check",
                "Delete the stripe check for part “%s”?\n\n"
                "Camera 2 will no longer check this part on the line." % pno,
                parent=parent):
            return
        stripe_check.delete_model(pno)
        _refresh_stripes()

    def _configure_cam2():
        if _open_camera_dialog(parent, stripe_check.CAMERA_SOURCE,
                               "Stripe Camera (Camera 2)"):
            _refresh_stripes()

    btn_s_teach.configure(command=lambda: _stripe_teach(None))
    btn_s_reteach.configure(command=lambda: _stripe_teach(_stripe_selection()))
    btn_s_test.configure(command=_stripe_test)
    btn_s_del.configure(command=_stripe_delete)
    btn_cam2_cfg.configure(command=_configure_cam2)
    stripe_tree.bind("<<TreeviewSelect>>", _on_stripe_select)
    stripe_tree.bind("<Double-1>", lambda e: _stripe_test())

    # ── Right rail: part coverage ────────────────────────────────────────────
    # The parts table audits datasets — files that exist and whether they're
    # wired up. This audits the other direction: real parts in the master that
    # the line will build with no vision dataset at all, which is the actual
    # production exposure, not a stray file on disk.
    cov_card = _card(rail, "Part Coverage", icon="clipboard")
    cov_card.pack(fill="x", padx=(0, 6), pady=(12, 0))
    cvb = cov_card.body
    tk.Label(cvb, text="Parts in the model master with no vision dataset.",
             bg=PANEL, fg=TXT_FAINT, font=(FONT, 10), anchor="w", justify="left",
             wraplength=265).pack(fill="x")
    cov_summary = tk.Label(cvb, text="Checking…", bg=PANEL, fg=TXT_DIM,
                           font=(FONT, 11, "bold"), anchor="w")
    cov_summary.pack(fill="x", pady=(4, 0))
    cov_list = tk.Listbox(cvb, font=(MONO, 11), height=6,
                          selectmode="browse", activestyle="none")
    cov_list.pack(fill="x", pady=(6, 0))

    def _refresh_coverage():
        if not alive["page"]:
            return
        master_parts = _fetch_master_parts()
        cov_list.delete(0, "end")
        if master_parts is None:
            cov_summary.config(text="Model master unreachable", fg=WARN)
            cov_list.insert("end", "  Could not reach the database.")
            return
        if not master_parts:
            cov_summary.config(text="No parts in the master yet", fg=TXT_FAINT)
            return
        mapped = set(ctrl.get_mapped_parts())
        missing = [pno for pno, _ in master_parts if pno not in mapped]
        covered = len(master_parts) - len(missing)
        cov_summary.config(text="%d of %d parts have vision" % (covered, len(master_parts)),
                           fg=OK_GREEN if not missing else WARN)
        for pno in missing:
            cov_list.insert("end", "  " + pno)

    # ── Right rail: inspection settings ────────────────────────────────────
    insp_card = _card(rail, "Inspection", icon="gear")
    insp_card.pack(fill="x", padx=(0, 6), pady=(12, 0))
    ib = insp_card.body

    enabled_var = tk.BooleanVar(value=v_cfg.get("vision_enabled", True))
    thresh_var = tk.DoubleVar(
        value=float(v_cfg.get("match_threshold", DEFAULT_MATCH_THRESHOLD)))
    initial = (enabled_var.get(), round(thresh_var.get(), 2))

    ttk.Checkbutton(ib, text="Camera 1 check enabled", variable=enabled_var).pack(fill="x")
    tk.Label(ib, text="Also in camera 1's setup on the Test console. When off, camera 1 "
                      "doesn't check the parts; camera 2 is turned on or off in its own "
                      "setup there.",
             bg=PANEL, fg=TXT_FAINT, font=(FONT, 10), anchor="w",
             wraplength=265, justify="left").pack(fill="x", padx=(22, 0), pady=(0, 12))

    tk.Frame(ib, bg=LINE, height=1).pack(fill="x", pady=(0, 12))

    th_head = tk.Frame(ib, bg=PANEL)
    th_head.pack(fill="x")
    tk.Label(th_head, text="Default match threshold", bg=PANEL, fg=TXT,
             font=(FONT, 11, "bold")).pack(side="left")
    th_val = tk.Label(th_head, text="0.75", bg=PANEL, fg=ACCENT,
                      font=(MONO, 14, "bold"))
    th_val.pack(side="right")

    scale = ttk.Scale(ib, from_=0.40, to=0.99, orient="horizontal",
                      variable=thresh_var)
    scale.pack(fill="x", pady=(6, 2))

    ticks = tk.Frame(ib, bg=PANEL)
    ticks.pack(fill="x")
    tk.Label(ticks, text="lenient", bg=PANEL, fg=TXT_FAINT,
             font=(FONT, 10)).pack(side="left")
    tk.Label(ticks, text="strict", bg=PANEL, fg=TXT_FAINT,
             font=(FONT, 10)).pack(side="right")

    th_caption = tk.Label(ib, text="", bg=PANEL, fg=TXT_DIM, font=(FONT, 10),
                          wraplength=265, justify="left", anchor="w")
    th_caption.pack(fill="x", pady=(8, 0))

    tk.Label(ib, text="Applies to parts taught from now on. Each taught part keeps "
                      "the threshold it was saved with — change one from the table.",
             bg=PANEL, fg=TXT_FAINT, font=(FONT, 10), wraplength=265,
             justify="left", anchor="w").pack(fill="x", pady=(8, 0))

    save_row = tk.Frame(ib, bg=PANEL)
    save_row.pack(fill="x", pady=(12, 0))
    btn_save = _btn(save_row, "Save Settings", BTN_PRIMARY, icon="check")
    btn_save.pack(side="right")
    dirty_lbl = tk.Label(save_row, text="", bg=PANEL, fg=WARN, font=(FONT, 11, "bold"))
    dirty_lbl.pack(side="left")

    tk.Frame(rail, bg=BG, height=12).pack(fill="x")

    def _on_settings_change(*_a):
        val = round(thresh_var.get(), 2)
        th_val.config(text="%.2f" % val)
        caption, color = _threshold_caption(val)
        th_caption.config(text=caption, fg=color)
        changed = (enabled_var.get(), val) != initial
        dirty_lbl.config(text="Unsaved changes" if changed else "", fg=WARN)
        _set_btn_enabled(btn_save, changed)

    thresh_var.trace_add("write", _on_settings_change)
    enabled_var.trace_add("write", _on_settings_change)

    def _save_settings():
        nonlocal initial
        val = round(thresh_var.get(), 2)
        v_cfg.update(load_vision_config())     # keep what the dialogs saved
        v_cfg["vision_enabled"] = enabled_var.get()
        v_cfg["match_threshold"] = val
        try:
            save_vision_config(v_cfg)
        except OSError as e:
            messagebox.showerror("Vision Settings", "Could not save:\n\n%s" % e,
                                 parent=parent)
            return
        ctrl.reload_config()
        initial = (enabled_var.get(), val)
        _set_btn_enabled(btn_save, False)
        dirty_lbl.config(text="Saved", fg=OK_GREEN)
        parent.after(1800, lambda: dirty_lbl.config(text="", fg=WARN)
                     if alive["page"] else None)

    btn_save.configure(command=_save_settings)
    _on_settings_change()

    # ── Row actions ────────────────────────────────────────────────────────

    def _teach(part_number=None):
        cam = _load_cam_cfg()
        saved = _open_teach_wizard(parent, cam, part_number)
        if saved:
            _refresh_table(select=saved)

    def _reteach():
        kind, value = _selection()
        if kind == "part":
            _teach(value)

    def _run_test():
        kind, value = _selection()
        if kind == "part" and ctrl.model_info(value) is not None:
            _open_test_dialog(parent, ctrl, value, on_changed=_refresh_table)

    def _set_threshold():
        kind, value = _selection()
        if kind != "part":
            return
        info = ctrl.model_info(value)
        if info is None:
            return
        if _open_threshold_dialog(parent, ctrl, value, info["threshold"]):
            _refresh_table(select=value)

    def _map_orphan():
        kind, filename = _selection()
        if kind != "orphan":
            return
        pno = _prompt_part_number(
            parent, "Map Model File",
            "Part number that should use “%s”:" % filename,
            taken=set(ctrl.get_mapped_parts()))
        if not pno:
            return
        try:
            ctrl.map_model_file(pno, filename)
        except ValueError as e:
            messagebox.showerror("Map Model", str(e), parent=parent)
            return
        _refresh_table(select=pno)

    def _delete():
        kind, value = _selection()
        if kind == "orphan":
            if not messagebox.askyesno(
                    "Delete Model File",
                    "Permanently delete the unmapped file “%s”?" % value,
                    parent=parent):
                return
            try:
                os.remove(os.path.join(_MODELS_DIR, value))
            except OSError as e:
                messagebox.showerror("Delete", str(e), parent=parent)
                return
        elif kind == "part":
            if not messagebox.askyesno(
                    "Delete Dataset",
                    "Delete the vision dataset for part “%s”?\n\n"
                    "The part will no longer be checked by vision on the line."
                    % value, parent=parent):
                return
            ctrl.delete_model(value)
        else:
            return
        _refresh_table()

    btn_teach = _btn(toolbar, "Teach New Part", BTN_SUCCESS, icon="box",
                     command=lambda: _teach(None), pady=7)
    btn_teach.pack(side="left")
    btn_reteach = _btn(toolbar, "Re-teach", BTN_NEUTRAL, icon="refresh",
                       command=_reteach, pady=7)
    btn_reteach.pack(side="left", padx=(8, 0))
    btn_test = _btn(toolbar, "Run Test", BTN_PRIMARY, icon="play",
                    command=_run_test, pady=7)
    btn_test.pack(side="left", padx=(8, 0))
    btn_thresh = _btn(toolbar, "Threshold…", BTN_NEUTRAL, icon="ruler",
                      command=_set_threshold, pady=7)
    btn_thresh.pack(side="left", padx=(8, 0))
    btn_del = _btn(toolbar, "Delete", BTN_DANGER, icon="alert",
                   command=_delete, pady=7)
    btn_del.pack(side="left", padx=(8, 0))
    btn_map.configure(command=_map_orphan)

    def _on_select(event=None):
        kind, value = _selection()
        is_part = kind == "part"
        info = ctrl.model_info(value) if is_part else None
        usable = info is not None
        for b, on in ((btn_reteach, is_part), (btn_test, usable),
                      (btn_thresh, usable), (btn_del, kind is not None)):
            _set_btn_enabled(b, on)

        if kind == "orphan":
            detail_lbl.config(
                text="Not mapped to any part number — production cannot use this file.",
                fg=WARN)
            btn_map.pack(side="right")
        else:
            btn_map.pack_forget()
            if is_part and not usable:
                detail_lbl.config(
                    text="Mapped file is missing from vision_models — re-teach this part.",
                    fg=WARN)
            elif usable and info["threshold"] < 0.55:
                detail_lbl.config(
                    text="This part passes at a %.2f match — near enough to accept any "
                         "frame. Raise it with “Threshold…”." % info["threshold"],
                    fg=WARN)
            elif is_part:
                detail_lbl.config(
                    text="Run Test captures a live frame and judges it exactly as the "
                         "test cycle does.", fg=TXT_FAINT)
            else:
                detail_lbl.config(text="", fg=TXT_FAINT)

    tree.bind("<<TreeviewSelect>>", _on_select)
    tree.bind("<Double-1>", lambda e: _run_test())
    tree.bind("<Delete>", lambda e: _delete())

    # ── Teardown ───────────────────────────────────────────────────────────
    def _on_destroy(event):
        if event.widget is content:
            alive["page"] = False

    content.bind("<Destroy>", _on_destroy)

    _refresh_table()
    _refresh_camera()
    _refresh_stripes()


# ═══════════════════════════════════════════════════════════════════════════════
# Teach wizard
# ═══════════════════════════════════════════════════════════════════════════════

MIN_REFS = 3
MAX_REFS = 12


def _dialog(parent, title, width, height):
    """Modal toplevel, centred on the app window, styled like the page."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=BG)
    win.transient(parent.winfo_toplevel())
    win.resizable(True, True)
    root = parent.winfo_toplevel()
    root.update_idletasks()
    # Never larger than the screen, or the footer buttons fall off it.
    width = min(width, win.winfo_screenwidth() - 40)
    height = min(height, win.winfo_screenheight() - 80)
    x = root.winfo_rootx() + (root.winfo_width() - width) // 2
    y = root.winfo_rooty() + (root.winfo_height() - height) // 3
    win.geometry("%dx%d+%d+%d" % (width, height, max(x, 0), max(y, 0)))
    win.grab_set()
    return win


def _dialog_header(win, title, subtitle):
    bar = tk.Frame(win, bg=ui.NAVY)
    bar.pack(fill="x")
    inner = tk.Frame(bar, bg=ui.NAVY)
    inner.pack(fill="x", padx=18, pady=10)
    tk.Label(inner, text=title, bg=ui.NAVY, fg=ui.TEXT_ON_DARK,
             font=(FONT, 15, "bold")).pack(anchor="w")
    tk.Label(inner, text=subtitle, bg=ui.NAVY, fg=ui.ACCENT_SOFT,
             font=(FONT, 11)).pack(anchor="w")
    return bar


def _dialog_footer(win):
    """The strip of action buttons along the foot of a dialog. Returns its body.

    Packed from the bottom before the body, so it keeps its height however much
    the body asks for.
    """
    foot = tk.Frame(win, bg=PANEL)
    foot.pack(side="bottom", fill="x")
    tk.Frame(win, bg=LINE, height=1).pack(side="bottom", fill="x")
    foot_in = tk.Frame(foot, bg=PANEL)
    foot_in.pack(fill="x", padx=18, pady=12)
    return foot_in


def _step(parent, number, title):
    """Numbered step block in the wizard rail. Returns its body frame."""
    wrap = tk.Frame(parent, bg=BG)
    wrap.pack(fill="x", pady=(0, 14))
    head = tk.Frame(wrap, bg=BG)
    head.pack(fill="x")
    badge = tk.Label(head, text=str(number), bg=ui.SUBTLE, fg=TXT,
                     font=(FONT, 11, "bold"), width=3)
    badge.pack(side="left")
    tk.Label(head, text=title, bg=BG, fg=TXT,
             font=(FONT, 12, "bold")).pack(side="left", padx=(8, 0))
    body = tk.Frame(wrap, bg=BG)
    body.pack(fill="x", padx=(34, 0), pady=(6, 0))
    wrap.badge = badge
    return body, badge


def _open_teach_wizard(parent, cam, part_number=None):
    """Teach or re-teach one part. Returns the saved part number, or None."""
    if not _cv2_ok or not _pil_ok:
        messagebox.showerror("Vision", "OpenCV and Pillow are required to teach a part.",
                             parent=parent)
        return None

    from vision_engine import VisionController
    from vision_engine.config import DEFAULT_MATCH_THRESHOLD
    from vision_engine import camera

    ctrl = VisionController()
    existing_parts = set(ctrl.get_mapped_parts())
    reteach = part_number is not None
    # Re-teaching keeps whatever threshold the part was tuned to; only a brand
    # new part inherits the page default.
    info = ctrl.model_info(part_number) if reteach else None
    threshold = (info or {}).get(
        "threshold", ctrl.config.get("match_threshold", DEFAULT_MATCH_THRESHOLD))

    win = _dialog(parent, "Teach Part", 1120, 720)
    _dialog_header(
        win,
        "Re-teach “%s”" % part_number if reteach else "Teach New Part",
        "Capture the good part a few times, then box it on every reference.")

    alive = {"v": True}
    refs = []                       # [{"img", "shown", "label", "thumb", "roi"}]
    sel = {"i": None}
    live = {"on": False}
    stream = {"s": None}
    ref_size = {"wh": None}

    # ── Footer: checklist + actions ────────────────────────────────────────
    # Packed before the body: the packer serves slaves in packing order, so a
    # body packed first claims the height it wants and leaves the footer with
    # the remainder — which collapsed these buttons to a sliver as soon as the
    # reference strip grew. Claiming the footer's space up front keeps them
    # whole no matter how many references are loaded.
    foot_in = _dialog_footer(win)

    checklist = tk.Label(foot_in, text="", bg=PANEL, fg=TXT_DIM, font=(MONO, 11),
                         anchor="w", justify="left")
    checklist.pack(side="left")

    btn_save = _btn(foot_in, "Save Dataset", BTN_SUCCESS, font_size=12, pady=8,
                    icon="check")
    btn_save.pack(side="right")
    btn_cancel = _btn(foot_in, "Cancel", BTN_NEUTRAL, font_size=12, pady=8)
    btn_cancel.pack(side="right", padx=(0, 8))

    body = tk.Frame(win, bg=BG)
    body.pack(fill="both", expand=True, padx=14, pady=12)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(0, weight=1)

    # ── Left: image view + view toolbar ────────────────────────────────────
    left = tk.Frame(body, bg=BG)
    left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
    left.rowconfigure(0, weight=1)
    left.columnconfigure(0, weight=1)

    view_wrap = tk.Frame(left, bg=LINE)
    view_wrap.grid(row=0, column=0, sticky="nsew")
    view = RoiView(view_wrap, on_change=lambda r, final: _roi_changed(r, final),
                   enhanced=True)
    view.pack(fill="both", expand=True, padx=1, pady=1)

    view_bar = tk.Frame(left, bg=BG)
    view_bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
    frame_lbl = tk.Label(view_bar, text="", bg=BG, fg=TXT_DIM, font=(MONO, 11))
    frame_lbl.pack(side="right")
    btn_live = _btn(view_bar, "Live View", BTN_NEUTRAL, icon="play")
    btn_capture = _btn(view_bar, "Capture Frame", BTN_SUCCESS, icon="camera")
    btn_import = _btn(view_bar, "Import Files…", BTN_NEUTRAL, icon="document")

    # ── Right: steps ───────────────────────────────────────────────────────
    # Scrollable: at a full dozen references the thumbnail and crop strips are
    # taller than the window, and the crops are the one place an operator can
    # see that a box missed the part — they have to stay reachable.
    # 356 = the 34px step indent + a 4-wide thumbnail strip (304px) + the
    # scrollbar, so the fourth column isn't sliced off.
    rail_wrap = tk.Frame(body, bg=BG, width=356)
    rail_wrap.grid(row=0, column=1, sticky="ns")
    rail_wrap.pack_propagate(False)

    rail_canvas = tk.Canvas(rail_wrap, bg=BG, highlightthickness=0, bd=0)
    rail_sb = ttk.Scrollbar(rail_wrap, orient="vertical", command=rail_canvas.yview)
    rail_canvas.configure(yscrollcommand=rail_sb.set)
    rail_sb.pack(side="right", fill="y")
    rail_canvas.pack(side="left", fill="both", expand=True)

    rail = tk.Frame(rail_canvas, bg=BG)
    rail_window = rail_canvas.create_window((0, 0), window=rail, anchor="nw")

    def _rail_resized(_event=None):
        rail_canvas.configure(scrollregion=rail_canvas.bbox("all"))
        rail_canvas.itemconfigure(rail_window, width=rail_canvas.winfo_width())

    rail.bind("<Configure>", _rail_resized)
    rail_canvas.bind("<Configure>", _rail_resized)

    def _rail_wheel(event):
        try:
            rail_canvas.yview_scroll(-1 * (event.delta // 120), "units")
        except Exception:
            pass

    # Bound while the pointer is over the rail rather than globally, so the
    # wheel keeps working normally everywhere else.
    rail_canvas.bind("<Enter>", lambda e: rail_canvas.bind_all("<MouseWheel>", _rail_wheel))
    rail_canvas.bind("<Leave>", lambda e: rail_canvas.unbind_all("<MouseWheel>"))

    s1, b1 = _step(rail, 1, "Part number")
    master_parts = _fetch_master_parts()
    ent_pno = _PnoField(s1, master_parts)
    ent_pno.pack(fill="x", ipady=5)
    pno_note = tk.Label(s1, text="", bg=BG, fg=TXT_FAINT, font=(FONT, 10),
                        anchor="w", wraplength=270, justify="left")
    pno_note.pack(fill="x", pady=(4, 0))
    if master_parts is None:
        tk.Label(s1, text="Could not reach the model master — a typed value won't "
                          "be checked against it.",
                 bg=BG, fg=WARN, font=(FONT, 10), wraplength=270,
                 justify="left", anchor="w").pack(fill="x", pady=(2, 0))
    if reteach:
        ent_pno.set(part_number)
        ent_pno.lock()
        pno_note.config(text="Saving replaces the existing dataset for this part.",
                        fg=WARN)
    else:
        ent_pno.focus_set()

    s2, b2 = _step(rail, 2, "Reference images")
    tk.Label(s2, text="Capture the same good part %d+ times — vary position and "
                      "lighting slightly, the way the line will." % MIN_REFS,
             bg=BG, fg=TXT_FAINT, font=(FONT, 10), wraplength=270,
             justify="left", anchor="w").pack(fill="x", pady=(0, 8))
    refs_count = tk.Label(s2, text="", bg=BG, fg=TXT_DIM, font=(FONT, 11, "bold"),
                          anchor="w")
    refs_count.pack(fill="x")
    thumbs = tk.Frame(s2, bg=BG)
    thumbs.pack(fill="x", pady=(6, 0))

    s3, b3 = _step(rail, 3, "Target box")
    tk.Label(s3, text="Box the part on every reference. The box carries over to the "
                      "next image — click to drop it on the part there.",
             bg=BG, fg=TXT_FAINT, font=(FONT, 10), wraplength=270,
             justify="left", anchor="w").pack(fill="x", pady=(0, 8))
    roi_row = tk.Frame(s3, bg=BG)
    roi_row.pack(fill="x")
    roi_lbl = tk.Label(roi_row, text="Not drawn", bg=BG, fg=WARN,
                       font=(MONO, 12, "bold"), anchor="w")
    roi_lbl.pack(side="left")
    btn_clear_roi = _btn(roi_row, "Clear", BTN_NEUTRAL, pady=3, font_size=10,
                         command=lambda: view.clear_roi())
    btn_clear_roi.pack(side="right")

    lock_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(s3, text="Same size on every reference", variable=lock_var,
                    command=lambda: _apply_lock()).pack(fill="x", pady=(8, 0))
    tk.Label(s3, text="Matching is not scale-invariant, so crops of different sizes "
                      "are not directly comparable. Unlock only if the part changes "
                      "size between references.",
             bg=BG, fg=TXT_FAINT, font=(FONT, 10), wraplength=270,
             justify="left", anchor="w").pack(fill="x", pady=(2, 0))

    tk.Label(s3, text="TEMPLATES", bg=BG, fg=TXT_DIM,
             font=(FONT, 10, "bold"), anchor="w").pack(fill="x", pady=(10, 2))
    tk.Label(s3, text="The actual pixels each reference contributes — a crop showing "
                      "background means that box missed the part.",
             bg=BG, fg=TXT_FAINT, font=(FONT, 10), wraplength=270,
             justify="left", anchor="w").pack(fill="x", pady=(0, 6))
    crops = tk.Frame(s3, bg=BG)
    crops.pack(fill="x")

    result = {"saved": None}

    # ── Behaviour ──────────────────────────────────────────────────────────

    def _boxed():
        return [r for r in refs if r["roi"]]

    def _gates():
        pno = ent_pno.get().strip()
        return {
            "Part number": bool(pno),
            "%d+ references" % MIN_REFS: len(refs) >= MIN_REFS,
            "Box on every reference": bool(refs) and len(_boxed()) == len(refs),
        }

    def _refresh_gates(*_a):
        gates = _gates()
        checklist.config(text="   ".join(
            ("✓ " if ok else "○ ") + name for name, ok in gates.items()))
        _set_btn_enabled(btn_save, all(gates.values()))

        for badge, ok in zip((b1, b2, b3), gates.values()):
            badge.config(bg=OK_GREEN if ok else ui.SUBTLE,
                         fg=ui.TEXT_ON_DARK if ok else TXT)

        n = len(refs)
        refs_count.config(
            text="%d captured%s" % (n, "" if n >= MIN_REFS
                                    else "  ·  %d more needed" % (MIN_REFS - n)),
            fg=OK_GREEN if n >= MIN_REFS else WARN)

        if not reteach:
            pno = ent_pno.get().strip()
            if pno and pno in existing_parts:
                pno_note.config(text="“%s” is already taught — saving replaces it."
                                     % pno, fg=WARN)
            else:
                pno_note.config(text="", fg=TXT_FAINT)

    def _apply_lock():
        """Pin the box size to the first box drawn, unless the operator opts out."""
        first = next((r["roi"] for r in refs if r["roi"]), None)
        if lock_var.get() and first:
            view.lock_size((first["width"], first["height"]))
        else:
            view.lock_size(None)

    def _roi_changed(roi, final=True):
        i = sel["i"]
        if i is not None and 0 <= i < len(refs):
            refs[i]["roi"] = roi
        if roi:
            roi_lbl.config(text="%d × %d px" % (roi["width"], roi["height"]),
                           fg=OK_GREEN)
        else:
            roi_lbl.config(text="Not drawn", fg=WARN)
        _set_btn_enabled(btn_clear_roi, roi is not None)
        if not final:
            return          # mid-drag: the readout is live, the strips are not
        _apply_lock()
        _paint_thumbs()
        _paint_crops()
        _refresh_gates()

    def _set_live(on):
        live["on"] = on and stream["s"] is not None
        if live["on"]:
            sel["i"] = None
            view.set_roi(None, notify=False)
            view.set_editable(False)
            view.set_hint("Live view — capture a frame to draw the target box")
            frame_lbl.config(text="LIVE  ·  camera %d" % cam["index"])
        else:
            view.set_editable(True)
            view.set_hint("Drag to box the part")
        _paint_buttons()

    def _show_ref(i):
        if not (0 <= i < len(refs)):
            return
        live["on"] = False
        sel["i"] = i
        # Seed from the nearest reference that already has a box, in either
        # direction, so the operator nudges an existing box onto the part instead
        # of redrawing it — whatever order they work through the images in.
        if refs[i]["roi"] is None:
            near = min((j for j in range(len(refs)) if refs[j]["roi"]),
                       key=lambda j: abs(j - i), default=None)
            if near is not None:
                refs[i]["roi"] = dict(refs[near]["roi"])
        view.set_image(refs[i]["img"])
        _apply_lock()
        view.set_roi(refs[i]["roi"], notify=False)
        view.set_editable(True)
        view.set_hint("Click to place the box on the part"
                      if view._locked_size else "Drag to box the part")
        frame_lbl.config(text="REFERENCE %d of %d  ·  %s"
                              % (i + 1, len(refs), refs[i]["label"]))
        _roi_changed(refs[i]["roi"])
        _paint_thumbs()
        _paint_buttons()

    def _paint_buttons():
        has_cam = stream["s"] is not None
        btn_live.pack_forget(); btn_capture.pack_forget(); btn_import.pack_forget()
        if has_cam:
            btn_capture.pack(side="left")
            btn_live.pack(side="left", padx=(8, 0))
            _set_btn_enabled(btn_live, not live["on"])
            btn_import.pack(side="left", padx=(8, 0))
        else:
            btn_import.pack(side="left")

    def _paint_thumbs():
        for w in thumbs.winfo_children():
            w.destroy()
        for i, ref in enumerate(refs):
            r, c = divmod(i, 4)
            selected = (i == sel["i"])
            # An unboxed reference is the one thing that blocks saving, so it is
            # marked on the strip rather than only in the checklist.
            edge = OK_GREEN if selected else (LINE if ref["roi"] else ui.WARNING)
            cell = tk.Frame(thumbs, bg=edge, cursor="hand2")
            cell.grid(row=r, column=c, padx=(0, 6), pady=(0, 6))
            holder = tk.Frame(cell, bg=VIEW_BG)
            holder.pack(padx=2, pady=2)
            lbl = tk.Label(holder, image=ref["thumb"], bd=0, bg=VIEW_BG, cursor="hand2")
            lbl.pack()
            for w in (cell, holder, lbl):
                w.bind("<Button-1>", lambda e, i=i: _show_ref(i))
            x = tk.Label(cell, text="✕", bg=edge, fg=ui.readable_on(edge),
                         font=(FONT, 9, "bold"), cursor="hand2")
            x.place(relx=1.0, rely=0.0, anchor="ne")
            x.bind("<Button-1>", lambda e, i=i: _remove_ref(i))

    def _crop(ref):
        r = ref["roi"]
        if not r:
            return None
        return ref["shown"][r["y"]:r["y"] + r["height"], r["x"]:r["x"] + r["width"]]

    def _paint_crops():
        for w in crops.winfo_children():
            w.destroy()
        drawn = 0
        for i, ref in enumerate(refs):
            patch = _crop(ref)
            if patch is None or patch.size == 0:
                continue
            r, c = divmod(drawn, 4)
            drawn += 1
            cell = tk.Frame(crops, bg=OK_GREEN if i == sel["i"] else LINE,
                            cursor="hand2")
            cell.grid(row=r, column=c, padx=(0, 6), pady=(0, 6))
            holder = tk.Frame(cell, bg=VIEW_BG, width=66, height=50)
            holder.pack_propagate(False)
            holder.pack(padx=2, pady=2)
            photo, _ = _to_photo(patch, 62, 46)
            ref["crop_photo"] = photo          # keep a reference alive
            lbl = tk.Label(holder, image=photo, bd=0, bg=VIEW_BG, cursor="hand2")
            lbl.pack(expand=True)
            for w in (cell, holder, lbl):
                w.bind("<Button-1>", lambda e, i=i: _show_ref(i))

    def _add_ref(img, label):
        if len(refs) >= MAX_REFS:
            messagebox.showinfo("References",
                                "%d reference images is the maximum." % MAX_REFS,
                                parent=win)
            return False
        h, w = img.shape[:2]
        if ref_size["wh"] is None:
            ref_size["wh"] = (w, h)
        elif (w, h) != ref_size["wh"]:
            return False
        # Shown as the template will be cut from it
        shown = enhance(img)
        thumb, _ = _to_photo(shown, 66, 50)
        refs.append({"img": img, "shown": shown, "label": label, "thumb": thumb,
                     "roi": None})
        return True

    def _remove_ref(i):
        if not (0 <= i < len(refs)):
            return
        refs.pop(i)
        if not refs:
            ref_size["wh"] = None
            view.lock_size(None)
            view.set_image(None)
            view.set_placeholder("No reference images yet")
            sel["i"] = None
            _set_live(stream["s"] is not None)
            _roi_changed(None)
        else:
            _show_ref(min(i, len(refs) - 1))
        _paint_thumbs()
        _paint_crops()
        _refresh_gates()

    def _capture():
        s = stream["s"]
        if s is None:
            return
        frame = s.latest() if live["on"] else s.read(timeout=3.0)
        if frame is None:
            messagebox.showwarning("Capture", "No frame from the camera yet.",
                                   parent=win)
            return
        if not _add_ref(frame.copy(), "live"):
            messagebox.showwarning(
                "Capture",
                "The camera changed resolution mid-session.\n\n"
                "Remove the existing references and start again.", parent=win)
            return
        _show_ref(len(refs) - 1)
        _refresh_gates()

    def _import():
        paths = filedialog.askopenfilenames(
            parent=win, title="Select Reference Images",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        if not paths:
            return
        skipped = []
        for p in paths:
            img = _read_image(p)
            if img is None:
                skipped.append((os.path.basename(p), "unreadable"))
                continue
            if not _add_ref(img, os.path.basename(p)):
                skipped.append((os.path.basename(p),
                                "%d×%d" % (img.shape[1], img.shape[0])))
        if refs:
            # Land on the first reference still needing a box, so the operator
            # works forward through them rather than starting at the end.
            _show_ref(next((i for i, r in enumerate(refs) if not r["roi"]), 0))
        _refresh_gates()
        if skipped:
            need = "%d×%d" % ref_size["wh"] if ref_size["wh"] else "the camera resolution"
            messagebox.showwarning(
                "Some files skipped",
                "Every reference must be %s so one template fits every frame:\n\n%s"
                % (need, "\n".join("  •  %s  (%s)" % s for s in skipped)),
                parent=win)

    btn_capture.configure(command=_capture)
    btn_import.configure(command=_import)
    btn_live.configure(command=lambda: _set_live(True))

    def _odd_crops():
        """Indices of crops that don't look like the others.

        A box left behind on background still produces a valid template, and
        max-of-N scoring means one background template is enough to pass an empty
        fixture. Correlating every crop against the first one catches that before
        it reaches the line.
        """
        patches = [_crop(r) for r in refs]
        if any(p is None or p.size == 0 for p in patches):
            return []
        grays = [cv2.cvtColor(p, cv2.COLOR_BGR2GRAY) if len(p.shape) == 3 else p
                 for p in patches]
        h, w = grays[0].shape[:2]
        odd = []
        for i, g in enumerate(grays[1:], start=1):
            probe = cv2.resize(g, (w, h)) if g.shape[:2] != (h, w) else g
            score = cv2.matchTemplate(grays[0], probe, cv2.TM_CCOEFF_NORMED)[0][0]
            if score < 0.35:
                odd.append((i, score))
        return odd

    def _save():
        pno = ent_pno.get().strip()
        rois = [r["roi"] for r in refs]
        if not (pno and len(refs) >= MIN_REFS and all(rois)):
            return
        if any(r["width"] < RoiView.MIN_ROI or r["height"] < RoiView.MIN_ROI
               for r in rois):
            messagebox.showerror("Target Box",
                                 "One of the boxes is too small to match reliably.",
                                 parent=win)
            return

        odd = _odd_crops()
        if odd:
            listing = "\n".join("  •  Reference %d  (similarity %.2f)" % (i + 1, s)
                                for i, s in odd)
            if not messagebox.askyesno(
                    "Check the Boxes",
                    "These crops do not resemble the first one:\n\n%s\n\n"
                    "That usually means the box missed the part on those images. "
                    "A template of plain background will match the empty fixture "
                    "and pass it.\n\nSave anyway?" % listing, parent=win):
                return

        if not reteach and pno in existing_parts and not messagebox.askyesno(
                "Replace Dataset",
                "“%s” already has a vision dataset.\n\nReplace it?" % pno, parent=win):
            return
        cw, ch = cam["width"], cam["height"]
        rw, rh = ref_size["wh"]
        if cam["enabled"] and cam["index"] >= 0 and (rw, rh) != (cw, ch):
            if not messagebox.askyesno(
                    "Resolution Mismatch",
                    "References are %d×%d but the camera is configured for %d×%d.\n\n"
                    "Inspection will fail if the template does not fit a live frame.\n\n"
                    "Save anyway?" % (rw, rh, cw, ch), parent=win):
                return
        try:
            ctrl.build_and_save_model(
                part_number=pno, images=[r["img"] for r in refs],
                roi=rois, match_threshold=float(threshold))
        except Exception as e:
            messagebox.showerror("Save Failed", str(e), parent=win)
            return
        result["saved"] = pno
        _close()

    btn_save.configure(command=_save)

    def _close():
        alive["v"] = False
        if stream["s"] is not None:
            try:
                stream["s"].release()
            except Exception:
                pass
            stream["s"] = None
        try:
            # Closing with the pointer over the rail would otherwise leave the
            # wheel bound to a destroyed canvas.
            rail_canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        try:
            win.grab_release()
        except Exception:
            pass
        win.destroy()

    btn_cancel.configure(command=_close)
    win.protocol("WM_DELETE_WINDOW", _close)
    win.bind("<Escape>", lambda e: _close())
    ent_pno.bind("<KeyRelease>", _refresh_gates)

    # ── Camera bring-up ────────────────────────────────────────────────────
    if cam["enabled"] and cam["index"] >= 0:
        stream["s"] = camera.acquire(cam["index"], cam["width"], cam["height"])
        view.set_placeholder("Starting camera %d…" % cam["index"])
        _set_live(True)
    else:
        view.set_placeholder("No camera configured\n\n"
                             "Import reference images, or set a camera up first.")
        _set_live(False)

    def _tick():
        if not alive["v"]:
            return
        s = stream["s"]
        if live["on"] and s is not None:
            frame = s.latest()
            if frame is not None:
                view.set_image(frame)
            elif not s.is_alive():
                live["on"] = False
                view.set_image(None)
                view.set_placeholder("Camera %d stopped responding" % cam["index"])
                frame_lbl.config(text="CAMERA UNAVAILABLE")
        try:
            win.after(60, _tick)
        except Exception:
            pass

    _paint_buttons()
    _roi_changed(None)
    _paint_crops()
    _refresh_gates()
    _tick()

    parent.wait_window(win)
    return result["saved"]


# ═══════════════════════════════════════════════════════════════════════════════
# Inspection test
# ═══════════════════════════════════════════════════════════════════════════════

_VERDICT_INK = {"OK": OK_GREEN, "NG": NG_RED, "ERROR": WARN}
_VERDICT_FILL = {"OK": ui.SUCCESS_SOFT, "NG": ui.DANGER_SOFT, "ERROR": ui.ROW_BAND}


def _draw_score_meter(canvas, score, threshold, verdict_color):
    """Horizontal 0–1 correlation bar with the threshold marked on it."""
    canvas.delete("all")
    w = max(canvas.winfo_width(), 1)
    h = canvas.winfo_height()
    top, bot = 8, h - 16

    canvas.create_rectangle(0, top, w, bot, fill=ui.SUBTLE, outline=LINE)
    if score is not None and score > 0:
        canvas.create_rectangle(0, top, w * min(max(score, 0.0), 1.0), bot,
                                fill=verdict_color, outline="")
    tx = w * min(max(threshold, 0.0), 1.0)
    canvas.create_line(tx, top - 4, tx, bot + 4, fill=TXT, width=2)
    canvas.create_text(tx, h - 5, text="threshold %.2f" % threshold,
                       fill=TXT_DIM, font=(MONO, 9),
                       anchor="e" if tx > w * 0.6 else "w")
    canvas.create_text(2, h - 5, text="0.0", fill=TXT_FAINT,
                       font=(MONO, 9), anchor="w")


def _open_test_dialog(parent, ctrl, part_number, on_changed=None):
    """Run the production inspect() path against the live camera, or a still image."""
    win = _dialog(parent, "Inspection Test", 900, 660)
    _dialog_header(win, "Inspection Test — %s" % part_number,
                   "Runs the same match path the test cycle uses — against the "
                   "live camera, or a still image you supply.")

    foot_in = _dialog_footer(win)

    verdict = tk.Frame(win, bg=ui.SUBTLE, height=54)
    verdict.pack(fill="x")
    verdict.pack_propagate(False)
    verdict_lbl = tk.Label(verdict, text="RUNNING…", bg=ui.SUBTLE, fg=TXT_DIM,
                           font=(FONT, 22, "bold"))
    verdict_lbl.pack(side="left", padx=18)
    verdict_note = tk.Label(verdict, text="", bg=ui.SUBTLE, fg=TXT_DIM,
                            font=(FONT, 11), anchor="e", justify="right")
    verdict_note.pack(side="right", padx=18)

    body = tk.Frame(win, bg=BG)
    body.pack(fill="both", expand=True, padx=14, pady=12)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(0, weight=1)

    view_wrap = tk.Frame(body, bg=LINE)
    view_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
    view = RoiView(view_wrap, editable=False)
    view.pack(fill="both", expand=True, padx=1, pady=1)
    view.set_placeholder("Capturing…")

    rail = tk.Frame(body, bg=BG, width=260)
    rail.grid(row=0, column=1, sticky="ns")
    rail.pack_propagate(False)

    metrics = _card(rail, "Result", icon="chart")
    metrics.pack(fill="x")
    mb = metrics.body
    m_source = _kv_row(mb, "Source", "Live camera", mono=False)
    m_score = _kv_row(mb, "Score", "—", mono=True)
    m_thresh = _kv_row(mb, "Threshold", "—", mono=True)
    m_time = _kv_row(mb, "Time", "—", mono=True)
    m_refs = _kv_row(mb, "References", "—", mono=True)
    m_tmpl = _kv_row(mb, "Template", "—", mono=True)

    meter = tk.Canvas(mb, bg=PANEL, height=34, highlightthickness=0, bd=0)
    meter.pack(fill="x", pady=(10, 0))

    hint = tk.Label(rail, text="", bg=BG, fg=TXT_DIM, font=(FONT, 10),
                    wraplength=240, justify="left", anchor="w")
    hint.pack(fill="x", pady=(12, 0))

    btn_close = _btn(foot_in, "Close", BTN_NEUTRAL, font_size=12, pady=8)
    btn_close.pack(side="right")
    btn_rerun = _btn(foot_in, "Run Again", BTN_PRIMARY, font_size=12, pady=8,
                     icon="refresh")
    btn_rerun.pack(side="right", padx=(0, 8))
    btn_source = _btn(foot_in, "Test Image…", BTN_NEUTRAL, font_size=12, pady=8,
                      icon="document")
    btn_source.pack(side="right", padx=(0, 8))
    btn_tune = _btn(foot_in, "Adjust Threshold…", BTN_NEUTRAL, font_size=12, pady=8,
                    icon="ruler")
    btn_tune.pack(side="left")

    alive = {"v": True}
    last = {"result": None}
    source = {"kind": "camera", "image": None, "label": None}
    busy = {"v": False}
    # A run asked for while one is still judging: done straight after it,
    # so switching the source mid-run is not silently dropped.
    pending = {"v": False}
    meter_thr = {"v": 0.0}

    def _paint_verdict(fill):
        verdict.config(bg=fill)
        for w_ in (verdict_lbl, verdict_note):
            w_.config(bg=fill)

    def _run():
        """Judge in the background: a cold camera takes a second or more to settle,
        and the dialog must keep painting while it does."""
        if not alive["v"]:
            return
        if busy["v"]:
            pending["v"] = True
            return
        busy["v"] = True
        verdict_lbl.config(text="RUNNING…", fg=TXT_DIM)
        verdict_note.config(text="")
        _paint_verdict(ui.SUBTLE)
        _set_btn_enabled(btn_rerun, False)

        ctrl.reload_config()
        frame = source["image"] if source["kind"] == "image" else None
        out = {}

        def _work():
            try:
                out["result"] = ctrl.inspect(part_number, frame=frame)
            except Exception as e:
                out["error"] = e

        worker = threading.Thread(target=_work, daemon=True)
        worker.start()

        def _wait():
            if not alive["v"]:
                return
            if worker.is_alive():
                win.after(80, _wait)
                return
            busy["v"] = False
            if pending["v"]:
                pending["v"] = False
                _run()
                return
            if "error" in out:
                _set_btn_enabled(btn_rerun, True)
                verdict_lbl.config(text="ERROR", fg=WARN)
                verdict_note.config(text=str(out["error"]), fg=WARN)
                _paint_verdict(ui.ROW_BAND)
                return
            _show(out["result"])

        _wait()

    def _show(result):
        last["result"] = result
        color = _VERDICT_INK.get(result.judgement, TXT_DIM)
        _paint_verdict(_VERDICT_FILL.get(result.judgement, ui.SUBTLE))
        verdict_lbl.config(text=result.judgement, fg=color)
        verdict_note.config(text=result.error or "Part found", fg=color)

        info = ctrl.model_info(part_number) or {}
        m_source.config(text="Live camera" if source["kind"] == "camera" else source["label"])
        m_score.config(text="%.4f" % result.match_score if result.match_score > 0 else "—",
                       fg=color)
        m_thresh.config(text="%.2f" % result.threshold if result.threshold else
                        "%.2f" % info.get("threshold", 0.0), fg=TXT)
        m_time.config(text="%d ms" % result.processing_time_ms, fg=TXT)
        m_refs.config(text=str(info.get("references", "—")), fg=TXT)
        tw, th = info.get("template_size", (0, 0))
        m_tmpl.config(text="%d x %d" % (tw, th) if tw else "—", fg=TXT)

        if result.frame is not None:
            view.set_image(result.frame)
            view.set_accent(color)
            if result.match_box:
                x, y, bw, bh = result.match_box
                view.set_roi({"x": x, "y": y, "width": bw, "height": bh}, notify=False)
            view.set_hint("Best match found in this " +
                          ("image" if source["kind"] == "image" else "frame"))
        else:
            view.set_image(None)
            view.set_placeholder(result.error or "No frame captured")

        thr = result.threshold or info.get("threshold", 0.0)
        meter_thr["v"] = thr
        _draw_score_meter(meter, result.match_score, thr, color)

        if result.judgement == "NG":
            hint.config(
                text="The best match scored %.2f against a %.2f threshold. If the part "
                     "is genuinely present and correct, either re-teach it with more "
                     "reference images or lower this part's threshold."
                     % (result.match_score, thr), fg=WARN)
        elif result.judgement == "ERROR":
            hint.config(text="Nothing was judged — fix the error above and run again.",
                        fg=WARN)
        else:
            hint.config(text="Headroom above threshold: %+.2f."
                             % (result.match_score - thr), fg=TXT_DIM)
        _set_btn_enabled(btn_rerun, True)
        _set_btn_enabled(btn_tune, bool(info))

    def _pick_image():
        if source["kind"] == "image":
            # Already testing an image — the button toggles back to the camera.
            source["kind"], source["image"], source["label"] = "camera", None, None
            btn_source.configure(text="Test Image…")
            _run()
            return

        path = filedialog.askopenfilename(
            parent=win, title="Select Test Image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        if not path:
            return
        img = _read_image(path)
        if img is None:
            messagebox.showerror("Test Image", "Could not read that image file.", parent=win)
            return
        source["kind"] = "image"
        source["image"] = img
        source["label"] = os.path.basename(path)
        btn_source.configure(text="Use Live Camera")
        _run()

    def _tune():
        info = ctrl.model_info(part_number)
        if info and _open_threshold_dialog(parent, ctrl, part_number,
                                           info["threshold"], anchor=win):
            if on_changed:
                on_changed()
            _run()

    def _close():
        alive["v"] = False
        try:
            win.grab_release()
        except Exception:
            pass
        win.destroy()

    btn_rerun.configure(command=_run)
    btn_source.configure(command=_pick_image)
    btn_tune.configure(command=_tune)
    btn_close.configure(command=_close)
    win.protocol("WM_DELETE_WINDOW", _close)
    win.bind("<Escape>", lambda e: _close())
    meter.bind("<Configure>", lambda e: last["result"] and _draw_score_meter(
        meter, last["result"].match_score,
        meter_thr["v"],
        _VERDICT_INK.get(last["result"].judgement, WARN)))

    win.after(120, _run)
    parent.wait_window(win)


# ═══════════════════════════════════════════════════════════════════════════════
# Small dialogs
# ═══════════════════════════════════════════════════════════════════════════════

def _open_threshold_dialog(parent, ctrl, part_number, current, anchor=None):
    """Edit one taught part's own threshold. Returns True if it was saved."""
    host = anchor or parent
    win = _dialog(host, "Match Threshold", 460, 330)
    _dialog_header(win, "Threshold — %s" % part_number,
                   "Only this part is affected.")

    saved = {"v": False}
    var = tk.DoubleVar(value=float(current))

    def _save():
        try:
            ctrl.set_model_threshold(part_number, round(var.get(), 2))
        except Exception as e:
            messagebox.showerror("Threshold", str(e), parent=win)
            return
        saved["v"] = True
        win.destroy()

    foot_in = _dialog_footer(win)
    _btn(foot_in, "Save", BTN_PRIMARY, command=_save, font_size=11,
         pady=8, icon="check").pack(side="right")
    _btn(foot_in, "Cancel", BTN_NEUTRAL, command=win.destroy, font_size=11,
         pady=8).pack(side="right", padx=(0, 8))

    body = tk.Frame(win, bg=BG)
    body.pack(fill="both", expand=True, padx=20, pady=16)

    row = tk.Frame(body, bg=BG)
    row.pack(fill="x")
    tk.Label(row, text="Match threshold", bg=BG, fg=TXT,
             font=(FONT, 12, "bold")).pack(side="left")
    val = tk.Label(row, text="%.2f" % current, bg=BG, fg=ACCENT,
                   font=(MONO, 17, "bold"))
    val.pack(side="right")

    ttk.Scale(body, from_=0.40, to=0.99, orient="horizontal",
              variable=var).pack(fill="x", pady=(8, 4))

    caption = tk.Label(body, text="", bg=BG, fg=TXT_DIM, font=(FONT, 11),
                       wraplength=400, justify="left", anchor="w")
    caption.pack(fill="x", pady=(6, 0))
    tk.Label(body, text="Lower it if good parts are being rejected; raise it if a "
                        "wrong or missing part still passes.",
             bg=BG, fg=TXT_FAINT, font=(FONT, 10), wraplength=400,
             justify="left", anchor="w").pack(fill="x", pady=(10, 0))

    def _upd(*_a):
        v = round(var.get(), 2)
        val.config(text="%.2f" % v)
        text, color = _threshold_caption(v)
        caption.config(text=text, fg=color)

    var.trace_add("write", _upd)
    _upd()

    win.bind("<Escape>", lambda e: win.destroy())

    host.wait_window(win)
    return saved["v"]


def _prompt_part_number(parent, title, prompt, taken=()):
    """Ask for a part number, backed by the part master when it's reachable.

    Returns the chosen value, or None.
    """
    win = _dialog(parent, title, 460, 270)
    _dialog_header(win, title, prompt)

    out = {"v": None}
    foot_in = _dialog_footer(win)

    body = tk.Frame(win, bg=BG)
    body.pack(fill="both", expand=True, padx=20, pady=18)
    master_parts = _fetch_master_parts()
    field = _PnoField(body, master_parts, font_size=14)
    field.pack(fill="x", ipady=6)
    field.focus_set()
    note = tk.Label(body, text="", bg=BG, fg=WARN, font=(FONT, 10), anchor="w",
                    wraplength=400, justify="left")
    note.pack(fill="x", pady=(6, 0))
    if master_parts is None:
        note.config(text="Could not reach the model master — a typed value won't "
                         "be checked against it.")

    def _check(*_a):
        v = field.get()
        if v and v in taken:
            note.config(text="“%s” is already mapped — saving will re-point it." % v)
        elif master_parts is None:
            note.config(text="Could not reach the model master — a typed value "
                             "won't be checked against it.")
        else:
            note.config(text="")

    def _ok(event=None):
        v = field.get()
        if not v:
            note.config(text="Enter a part number.")
            return
        out["v"] = v
        win.destroy()

    field.bind("<KeyRelease>", _check)
    field.bind("<Return>", _ok)
    win.bind("<Escape>", lambda e: win.destroy())

    _btn(foot_in, "OK", BTN_PRIMARY, command=_ok, font_size=11, pady=8).pack(side="right")
    _btn(foot_in, "Cancel", BTN_NEUTRAL, command=win.destroy, font_size=11,
         pady=8).pack(side="right", padx=(0, 8))

    parent.wait_window(win)
    return out["v"]


# ═══════════════════════════════════════════════════════════════════════════════
# Camera configuration
# ═══════════════════════════════════════════════════════════════════════════════

def _open_camera_dialog(parent, source=None, title="Inspection Camera",
                        check_key=None, allow_check_change=None):
    """Pick and verify a camera. Returns True if the config changed.

    `source` names the camera in camera_cfg.ini; by default it is the one
    template matching uses. `check_key` names the vision_config.json
    setting that turns this camera's check of the parts on or off, and adds
    a box for it; `allow_check_change(window)` is asked when the box is
    clicked, and returns False to leave it as it was.
    """
    if not _cv2_ok or not _pil_ok:
        messagebox.showerror("Camera", "OpenCV and Pillow are required.", parent=parent)
        return False

    from vision_engine import camera, load_vision_config, save_vision_config

    cam = _load_cam_cfg(source)

    # Taller with the camera's on/off box, so Re-scan still fits beneath it
    win = _dialog(parent, "Camera Configuration", 820, 680 if check_key else 580)
    _dialog_header(win, title, "The preview is the exact feed inspection will use.")

    alive = {"v": True}
    stream = {"s": None, "index": None}
    found = {"cams": []}
    changed = {"v": False}

    foot_in = _dialog_footer(win)
    btn_save = _btn(foot_in, "Save", BTN_PRIMARY, font_size=12, pady=8, icon="check")
    btn_save.pack(side="right")
    btn_cancel = _btn(foot_in, "Cancel", BTN_NEUTRAL, font_size=12, pady=8)
    btn_cancel.pack(side="right", padx=(0, 8))

    body = tk.Frame(win, bg=BG)
    body.pack(fill="both", expand=True, padx=14, pady=12)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(0, weight=1)

    prev_wrap = tk.Frame(body, bg=LINE)
    prev_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
    preview = RoiView(prev_wrap, editable=False)
    preview.pack(fill="both", expand=True, padx=1, pady=1)
    preview.set_placeholder("Scanning for cameras…")

    rail_scroll = ui.scrollable(body, bg=BG)
    rail_scroll.configure(width=290)
    rail_scroll.grid(row=0, column=1, sticky="ns")
    rail_scroll.grid_propagate(False)
    rail = rail_scroll.body

    card = _card(rail, "Device", icon="camera")
    card.pack(fill="x")
    cb_body = card.body

    # Whether this camera checks the parts at all
    check_var = tk.BooleanVar(value=True)
    check_was = None
    check_allowed = {"v": allow_check_change is None}

    def _on_check():
        # The tick only changes after a login
        if check_allowed["v"]:
            return
        wanted = check_var.get()
        check_var.set(not wanted)
        if allow_check_change(win):
            check_allowed["v"] = True
            check_var.set(wanted)

    if check_key:
        v_cfg = load_vision_config()
        # Before each camera had its own setting, one turned both on or off
        check_was = bool(v_cfg.get(check_key, v_cfg.get("vision_enabled", True)))
        check_var.set(check_was)
        ctk.CTkCheckBox(cb_body, text="Check parts with this camera", variable=check_var,
                        command=_on_check, bg_color=PANEL, fg_color=ui.NAVY, hover_color=ui.ACCENT_ACTIVE,
                        border_color=ui.BORDER_STRONG, text_color=TXT,
                        checkbox_width=22, checkbox_height=22, corner_radius=4,
                        font=(FONT, 11, "bold")).pack(fill="x")
        tk.Label(cb_body, text="When off, it takes no picture and does not judge the "
                               "part.",
                 bg=PANEL, fg=TXT_FAINT, font=(FONT, 10), wraplength=215,
                 justify="left", anchor="w").pack(fill="x", padx=(30, 0), pady=(2, 10))
        tk.Frame(cb_body, bg=LINE, height=1).pack(fill="x", pady=(0, 10))

    tk.Label(cb_body, text="Camera", bg=PANEL, fg=TXT_DIM, font=(FONT, 11),
             anchor="w").pack(fill="x")
    dev_var = tk.StringVar()
    cmb_dev = ttk.Combobox(cb_body, textvariable=dev_var, state="readonly",
                           values=["Scanning…"], font=(FONT, 11))
    cmb_dev.pack(fill="x", pady=(3, 10))

    tk.Label(cb_body, text="Resolution", bg=PANEL, fg=TXT_DIM, font=(FONT, 11),
             anchor="w").pack(fill="x")
    res_var = tk.StringVar()
    # The sizes the chosen camera delivers, once the scan has found them
    cmb_res = ttk.Combobox(cb_body, textvariable=res_var, state="readonly",
                           values=["%dx%d" % (cam["width"], cam["height"])],
                           font=(FONT, 11))
    cmb_res.pack(fill="x", pady=(3, 10))
    res_var.set("%dx%d" % (cam["width"], cam["height"]))

    tk.Label(cb_body, text="Reference images are captured at this resolution and "
                           "must keep matching it, so changing it later means "
                           "re-teaching every part.",
             bg=PANEL, fg=TXT_FAINT, font=(FONT, 10), wraplength=215,
             justify="left", anchor="w").pack(fill="x")

    btn_rescan = _btn(cb_body, "Re-scan", BTN_NEUTRAL, pady=5, icon="refresh")
    btn_rescan.pack(fill="x", pady=(10, 0))

    status = tk.Label(rail, text="", bg=BG, fg=TXT_DIM, font=(FONT, 11),
                      wraplength=250, justify="left", anchor="w")
    status.pack(fill="x", pady=(12, 0))

    DISABLED = "Disabled (no vision capture)"

    def _stop_stream():
        if stream["s"] is not None:
            try:
                stream["s"].release()
            except Exception:
                pass
            stream["s"] = None
            stream["index"] = None

    def _selected_index():
        label = dev_var.get()
        for c in found["cams"]:
            if label == _cam_label(c):
                return c["index"]
        return -1

    def _fit_sizes():
        """Offer the chosen camera's own sizes. The camera already set up
        keeps its size, as its parts were taught at it; another starts at
        its largest, for the sharpest picture."""
        idx = _selected_index()
        c = next((c for c in found["cams"] if c["index"] == idx), None)
        if c is None:
            return
        sizes = ["%dx%d" % s for s in c["sizes"]]
        saved = "%dx%d" % (cam["width"], cam["height"])
        cmb_res.config(values=sizes)
        if idx == cam["index"] and cam["enabled"] and saved in sizes:
            res_var.set(saved)
        else:
            res_var.set("%dx%d" % (c["width"], c["height"]))

    def _on_device_pick(*_a):
        _fit_sizes()
        _on_res_change()

    def _on_device_change(*_a):
        idx = _selected_index()
        _stop_stream()
        preview.set_image(None)
        if idx < 0:
            preview.set_placeholder("Camera disabled — vision will not run.")
            status.config(text="Vision inspection needs a camera.", fg=WARN)
            return
        w, h = [int(v) for v in res_var.get().split("x")]
        preview.set_placeholder("Opening camera %d…" % idx)
        status.config(text="", fg=TXT_DIM)
        stream["s"] = camera.acquire(idx, w, h)
        stream["index"] = idx

    def _on_res_change(*_a):
        _on_device_change()

    cmb_dev.bind("<<ComboboxSelected>>", _on_device_pick)
    cmb_res.bind("<<ComboboxSelected>>", _on_res_change)

    def _scan():
        _stop_stream()
        preview.set_image(None)
        preview.set_placeholder("Scanning for cameras…")
        cmb_dev.config(values=["Scanning…"], state="disabled")
        dev_var.set("Scanning…")
        _set_btn_enabled(btn_rescan, False)

        def _work():
            cams = _probe_cameras()

            def _apply():
                if not alive["v"]:
                    return
                found["cams"] = cams
                labels = [DISABLED] + [_cam_label(c) for c in cams]
                cmb_dev.config(values=labels, state="readonly")
                pick = DISABLED
                for lab, c in zip(labels[1:], cams):
                    if c["index"] == cam["index"] and cam["enabled"]:
                        pick = lab
                dev_var.set(pick)
                _set_btn_enabled(btn_rescan, True)
                if not cams:
                    preview.set_placeholder("No camera detected.\n\n"
                                            "Check the USB connection and re-scan.")
                    status.config(text="Nothing responded on indexes 0–5.", fg=NG_RED)
                else:
                    status.config(text="%d camera(s) detected." % len(cams), fg=TXT_DIM)
                _on_device_pick()

            try:
                win.after(0, _apply)
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()

    btn_rescan.configure(command=_scan)

    def _tick():
        if not alive["v"]:
            return
        s = stream["s"]
        if s is not None:
            frame = s.latest()
            if frame is not None:
                preview.set_image(frame)
                preview.set_hint("%d x %d" % (frame.shape[1], frame.shape[0]))
            elif not s.is_alive():
                preview.set_image(None)
                preview.set_placeholder("Camera %d is not delivering frames."
                                        % stream["index"])
                status.config(text="The device opened but produced no video. It may be "
                                   "in use by another program.", fg=NG_RED)
                _stop_stream()
        try:
            win.after(60, _tick)
        except Exception:
            pass

    def _save():
        idx = _selected_index()
        w, h = [int(v) for v in res_var.get().split("x")]
        check_now = check_var.get()
        if idx >= 0 and stream["s"] is not None and stream["s"].latest() is None:
            if not messagebox.askyesno(
                    "No Preview",
                    "No frames have arrived from camera %d yet.\n\nSave anyway?" % idx,
                    parent=win):
                return
        try:
            _save_cam_cfg(idx, w, h, idx >= 0, source)
            if check_key and check_now != check_was:
                v_cfg = load_vision_config()
                v_cfg[check_key] = check_now
                save_vision_config(v_cfg)
        except OSError as e:
            messagebox.showerror("Camera", "Could not save the camera:\n\n%s" % e,
                                 parent=win)
            return
        changed["v"] = True
        _close()

    def _close():
        alive["v"] = False
        _stop_stream()
        try:
            win.grab_release()
        except Exception:
            pass
        win.destroy()

    btn_save.configure(command=_save)
    btn_cancel.configure(command=_close)
    win.protocol("WM_DELETE_WINDOW", _close)
    win.bind("<Escape>", lambda e: _close())

    _scan()
    _tick()
    parent.wait_window(win)
    return changed["v"]


# ═══════════════════════════════════════════════════════════════════════════════
# Stripe check (camera 2)
# ═══════════════════════════════════════════════════════════════════════════════


def _open_stripe_teach(parent, part_number=None):
    """Teach or re-teach one part's stripes. Returns the saved part number, or None."""
    if not _cv2_ok or not _pil_ok:
        messagebox.showerror("Vision", "OpenCV and Pillow are required to teach a part.",
                             parent=parent)
        return None

    from vision_engine import camera
    from vision_engine.stripes import stripe_check

    cam = _load_cam_cfg(stripe_check.CAMERA_SOURCE)
    existing_parts = set(stripe_check.taught_parts())
    reteach = part_number is not None
    previous = stripe_check.load_model(part_number) if reteach else None

    def _as_box(rect):
        # A saved group's boxes are (x, y, w, h)
        return None if rect is None else dict(zip(("x", "y", "width", "height"), rect))

    win = _dialog(parent, "Teach Stripes", 1120, 720)
    _dialog_header(
        win,
        "Re-teach stripes — “%s”" % part_number if reteach else "Teach Stripes",
        "Capture a good part on camera 2, then box each group of stripes and the cable round it.")

    alive = {"v": True}
    live = {"on": False}
    stream = {"s": None}
    # The frame being taught from
    state = {"frame": None, "label": "", "loading": False}

    def _new_group(roi=None, area=None, body=None, colour=None, count=None):
        # "roi" is the box round the stripes and "area" the longer one round
        # the cable; "body", "colour" and "count" are what the check will
        # expect, kept when a box is redrawn; "model" and "outlines" are what
        # was read from the boxes
        return {"roi": roi, "area": area, "body": body, "colour": colour,
                "count": count, "model": None, "outlines": [], "error": None}

    groups = [_new_group()]
    # The group being filled in, or None when none is
    active = {"i": 0}
    # What dragging on the picture draws for that group: "box" or "area"
    draw = {"mode": "box"}
    result = {"saved": None}

    foot_in = _dialog_footer(win)
    checklist = tk.Label(foot_in, text="", bg=PANEL, fg=TXT_DIM, font=(MONO, 11),
                         anchor="w", justify="left")
    checklist.pack(side="left")
    btn_save = _btn(foot_in, "Save", BTN_SUCCESS, font_size=12, pady=8, icon="check")
    btn_save.pack(side="right")
    btn_cancel = _btn(foot_in, "Cancel", BTN_NEUTRAL, font_size=12, pady=8)
    btn_cancel.pack(side="right", padx=(0, 8))

    body = tk.Frame(win, bg=BG)
    body.pack(fill="both", expand=True, padx=14, pady=12)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(0, weight=1)

    # ── Left: image view + view toolbar ────────────────────────────────────
    left = tk.Frame(body, bg=BG)
    left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
    left.rowconfigure(0, weight=1)
    left.columnconfigure(0, weight=1)

    view_wrap = tk.Frame(left, bg=LINE)
    view_wrap.grid(row=0, column=0, sticky="nsew")
    # Zoomable: stripes are often only a few pixels wide in the frame, too
    # small to box accurately at the size that fits the whole picture
    # Shown as captured: the stripes are judged on the colours as they are
    view = RoiView(view_wrap, on_change=lambda r, final: _roi_changed(r, final),
                   zoomable=True, on_zoom=lambda z: _paint_zoom())
    # What to do next, in words, right above where it is done
    banner = tk.Label(view_wrap, text="", bg=ui.SUBTLE, fg=TXT, anchor="w",
                      font=(FONT, 13, "bold"), padx=12, pady=8)
    banner.pack(fill="x", padx=1, pady=(1, 0))
    view.pack(fill="both", expand=True, padx=1, pady=1)

    view_bar = tk.Frame(left, bg=BG)
    view_bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
    btn_capture = _btn(view_bar, "Capture Frame", BTN_SUCCESS, icon="camera")
    btn_live = _btn(view_bar, "Live View", BTN_NEUTRAL, icon="play")
    btn_import = _btn(view_bar, "Import Image…", BTN_NEUTRAL, icon="document")

    zoom_bar = tk.Frame(left, bg=BG)
    zoom_bar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
    tk.Label(zoom_bar, text="Zoom", bg=BG, fg=TXT_DIM, font=(FONT, 11)).pack(side="left")
    btn_zoom_out = _btn(zoom_bar, "−", BTN_NEUTRAL, width=34, pady=2,
                        command=lambda: view.zoom_by(1 / RoiView.ZOOM_STEP))
    btn_zoom_out.pack(side="left", padx=(8, 0))
    zoom_lbl = tk.Label(zoom_bar, text="1.0×", bg=BG, fg=TXT, width=6,
                        font=(MONO, 11, "bold"))
    zoom_lbl.pack(side="left", padx=4)
    btn_zoom_in = _btn(zoom_bar, "+", BTN_NEUTRAL, width=34, pady=2,
                       command=lambda: view.zoom_by(RoiView.ZOOM_STEP))
    btn_zoom_in.pack(side="left")
    btn_zoom_fit = _btn(zoom_bar, "Fit", BTN_NEUTRAL, pady=2, command=lambda: view.zoom_fit())
    btn_zoom_fit.pack(side="left", padx=(8, 0))
    frame_lbl = tk.Label(zoom_bar, text="", bg=BG, fg=TXT_DIM, font=(MONO, 11))
    frame_lbl.pack(side="right")

    def _paint_zoom():
        z = view.get_zoom()
        # Relative to the whole picture fitted in the view, not camera pixels
        zoom_lbl.config(text="%.1f×" % z)
        _set_btn_enabled(btn_zoom_out, z > 1.0)
        _set_btn_enabled(btn_zoom_in, z < RoiView.MAX_ZOOM)
        _set_btn_enabled(btn_zoom_fit, z > 1.0)

    # ── Right: steps ───────────────────────────────────────────────────────
    # Scrolls, so the colour and count stay reachable on a short screen
    rail_scroll = ui.scrollable(body, bg=BG)
    rail_scroll.configure(width=340)
    rail_scroll.grid(row=0, column=1, sticky="ns")
    rail_scroll.grid_propagate(False)
    rail = rail_scroll.body

    def _note(parent_, text, fg=TXT_FAINT):
        lbl = tk.Label(parent_, text=text, bg=BG, fg=fg, font=(FONT, 10),
                       wraplength=270, justify="left", anchor="w")
        lbl.pack(fill="x")
        return lbl

    s1, b1 = _step(rail, 1, "Part number")
    master_parts = _fetch_master_parts()
    ent_pno = _PnoField(s1, master_parts)
    ent_pno.pack(fill="x", ipady=5)
    pno_note = _note(s1, "")
    if master_parts is None:
        _note(s1, "Could not reach the model master — a typed value won't be "
                  "checked against it.", WARN)
    if reteach:
        ent_pno.set(part_number)
        ent_pno.lock()
        pno_note.config(text="Saving replaces this part's stripe check.", fg=WARN)
    else:
        ent_pno.focus_set()

    s2, b2 = _step(rail, 2, "Good part")
    _note(s2, "Put a good part in the fixture and capture it, or import a photo "
              "taken by camera 2.")
    frame_note = tk.Label(s2, text="", bg=BG, fg=TXT_DIM, font=(FONT, 11, "bold"),
                          anchor="w", justify="left", wraplength=270)
    frame_note.pack(fill="x", pady=(4, 0))

    s3, b3 = _step(rail, 3, "Stripe groups")
    _note(s3, "A group is one band of stripes of one colour, such as 3 yellow. "
              "Add a group for each band on the part and fill each one in. The "
              "cable must lie in the same place on every part, give or take a little.")
    group_rows = tk.Frame(s3, bg=BG)
    group_rows.pack(fill="x", pady=(6, 0))
    btn_add_grp = _btn(s3, "+ Add Group", BTN_NEUTRAL, pady=3,
                       command=lambda: _add_group())
    btn_add_grp.pack(anchor="w", pady=(6, 0))

    s4, b4 = _step(rail, 4, "Group")
    # _step doesn't hand back its title; it is the second thing in its head
    s4_title = s4.master.winfo_children()[0].winfo_children()[1]
    idle_note = _note(s4, "")

    editor = tk.Frame(s4, bg=BG)

    def _sub_head(text):
        tk.Label(editor, text=text, bg=BG, fg=TXT, font=(FONT, 11, "bold"),
                 anchor="w").pack(fill="x", pady=(8, 2))

    _sub_head("A   Box its stripes")
    box_note = _note(editor, "")
    btn_box = _btn(editor, "Redraw Stripe Box", BTN_NEUTRAL, pady=3,
                   command=lambda: _set_mode("box"))
    btn_box.pack(anchor="w", pady=(4, 0))

    _sub_head("B   Box the cable round them")
    area_note = _note(editor, "")
    btn_area = _btn(editor, "Redraw Cable Box", BTN_NEUTRAL, pady=3,
                    command=lambda: _set_mode("area"))
    btn_area.pack(anchor="w", pady=(4, 0))

    _sub_head("C   What the check expects")
    found_lbl = tk.Label(editor, text="—", bg=BG, fg=TXT_DIM, font=(FONT, 18, "bold"),
                         anchor="w")
    found_lbl.pack(fill="x")

    def _pick_row(label):
        row = tk.Frame(editor, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text=label, bg=BG, fg=TXT_DIM, font=(FONT, 11),
                 width=7, anchor="w").pack(side="left")
        return row

    body_var = tk.StringVar()
    cmb_body = ttk.Combobox(_pick_row("Cable"), textvariable=body_var, state="readonly",
                            values=list(stripe_check.COLOURS), width=12,
                            font=(FONT, 11))
    cmb_body.pack(side="left")
    pick_row = _pick_row("Stripes")
    colour_var = tk.StringVar()
    cmb_colour = ttk.Combobox(pick_row, textvariable=colour_var, state="readonly",
                              values=list(stripe_check.COLOURS), width=12,
                              font=(FONT, 11))
    cmb_colour.pack(side="left")
    tk.Label(pick_row, text="×", bg=BG, fg=TXT_DIM, font=(FONT, 11)).pack(
        side="left", padx=(8, 6))
    count_var = tk.StringVar()
    spn_count = ttk.Spinbox(pick_row, from_=1, to=9, width=4, textvariable=count_var,
                            font=(FONT, 11), state="readonly")
    spn_count.pack(side="left")
    count_note = _note(editor, "")
    found_note = _note(editor, "")

    btn_done = _btn(editor, "Done With This Group", BTN_PRIMARY, pady=5, icon="check",
                    command=lambda: _done_group())
    btn_done.pack(fill="x", pady=(14, 0))

    # ── Behaviour ──────────────────────────────────────────────────────────

    BOX_INK = ui.ACCENT_FILL
    AREA_INK = "#1E78C8"

    def _group():
        i = active["i"]
        return groups[i] if i is not None else None

    def _ready(g):
        return (g["model"] is not None and g["count"] == g["model"]["count"]
                and bool(g["body"]) and bool(g["colour"]))

    def _what(g):
        return "%s × %d on %s" % (g["colour"], g["count"] or g["model"]["count"], g["body"])

    def _status(g):
        """(text, colour) saying how far a group has got."""
        if g["roi"] is None:
            return "stripes not boxed yet", WARN
        if g["area"] is None:
            return "cable not boxed yet", WARN
        if g["model"] is None:
            return g["error"] or "no stripes read", WARN
        if not _ready(g):
            return _what(g) + "  ·  count doesn't match", WARN
        return _what(g), OK_GREEN

    def _count():
        try:
            return int(count_var.get())
        except ValueError:
            return None

    def _gates():
        return {
            "Part number": bool(ent_pno.get().strip()),
            "Good part": state["frame"] is not None,
            "Boxes": all(g["roi"] is not None and g["area"] is not None for g in groups),
            "Groups ready": all(_ready(g) for g in groups),
        }

    def _refresh_gates(*_a):
        gates = _gates()
        checklist.config(text="   ".join(
            ("✓ " if ok else "○ ") + name for name, ok in gates.items()))
        _set_btn_enabled(btn_save, all(gates.values()))
        for badge, ok in zip((b1, b2, b3, b4), gates.values()):
            badge.config(bg=OK_GREEN if ok else ui.SUBTLE,
                         fg=ui.TEXT_ON_DARK if ok else TXT)
        if not reteach:
            pno = ent_pno.get().strip()
            if pno and pno in existing_parts:
                pno_note.config(text="“%s” already has a stripe check — saving "
                                     "replaces it." % pno, fg=WARN)
            else:
                pno_note.config(text="", fg=TXT_FAINT)

    def _paint_groups():
        for w_ in group_rows.winfo_children():
            w_.destroy()
        for n, g in enumerate(groups, start=1):
            editing = n - 1 == active["i"]
            row = tk.Frame(group_rows, bg=PANEL if editing else BG,
                           highlightthickness=1,
                           highlightbackground=ui.ACCENT_FILL if editing else LINE)
            row.pack(fill="x", pady=(0, 4))
            text_col = tk.Frame(row, bg=row["bg"])
            text_col.pack(side="left", fill="x", expand=True, padx=8, pady=4)
            tk.Label(text_col, text="Group %d%s" % (n, "  (editing)" if editing else ""),
                     bg=row["bg"], fg=TXT, font=(FONT, 11, "bold"),
                     anchor="w").pack(fill="x")
            status, ink = _status(g)
            tk.Label(text_col, text=("✓ " if _ready(g) else "") + status, bg=row["bg"],
                     fg=ink, font=(FONT, 10), anchor="w", justify="left",
                     wraplength=170).pack(fill="x")
            if len(groups) > 1:
                _btn(row, "Remove", BTN_NEUTRAL, pady=2,
                     command=lambda i=n - 1: _remove_group(i)).pack(side="right", padx=(0, 6))
            if not editing:
                _btn(row, "Edit", BTN_NEUTRAL, pady=2,
                     command=lambda i=n - 1: _edit_group(i)).pack(side="right", padx=(0, 4))

    def _paint_editor():
        g = _group()
        if g is None:
            editor.pack_forget()
            s4_title.config(text="Group")
            idle_note.pack(fill="x")
            idle_note.config(
                text="Every group is ready. Save, or add another group." if
                all(_ready(x) for x in groups) else
                "Click Edit on a group that isn't ready.",
                fg=OK_GREEN if all(_ready(x) for x in groups) else WARN)
            return
        idle_note.pack_forget()
        editor.pack(fill="x")
        s4_title.config(text="Group %d" % (active["i"] + 1))

        if g["roi"] is None:
            box_note.config(text="Drag a box round this group's stripes on the picture, "
                                 "with a little cable either side.", fg=TXT_FAINT)
        else:
            box_note.config(text="Stripe box %d × %d px. Drag its edges on the picture "
                                 "to adjust it." % (g["roi"]["width"], g["roi"]["height"]),
                            fg=TXT_FAINT)
        if g["area"] is None:
            area_note.config(text="A longer box round the cable at the stripes, with some "
                                  "of the bench above and below it. The cable must run "
                                  "along its long side.", fg=TXT_FAINT)
        else:
            area_note.config(text="Cable box %d × %d px. Only the cable inside it is "
                                  "looked at; it must contain the stripe box."
                                  % (g["area"]["width"], g["area"]["height"]),
                             fg=TXT_FAINT)

        model = g["model"]
        state["loading"] = True
        body_var.set(g["body"] or "")
        colour_var.set(g["colour"] or "")
        if model is not None:
            count_var.set(str(g["count"] or model["count"]))
            found_lbl.config(text="%s × %d" % (model["colour"], model["count"]),
                             fg=OK_GREEN)
            found_note.config(
                text="Found on a %s cable. Each stripe is about %.0f px wide; the group "
                     "is %.0f px long. If a colour is named wrongly, correct it here."
                     % (model["body"], model["stripe_width"], model["span"]),
                fg=TXT_FAINT)
        else:
            count_var.set("")
            found_lbl.config(text="—", fg=TXT_DIM)
            found_note.config(text=g["error"] or "", fg=WARN)
        state["loading"] = False
        _check_count()

        _set_btn_enabled(btn_box, draw["mode"] != "box")
        _set_btn_enabled(btn_area, g["roi"] is not None and draw["mode"] != "area")
        _set_btn_enabled(btn_done, _ready(g))

    def _paint_banner():
        g = _group()
        n = (active["i"] or 0) + 1
        if live["on"]:
            text, fill = "Live view — put a good part in the fixture and press Capture Frame", ui.SUBTLE
        elif state["frame"] is None:
            text, fill = "Capture a good part, or import a photo of one", ui.SUBTLE
        elif g is None:
            if all(_ready(x) for x in groups):
                text, fill = "Every group is ready — press Save", ui.SUCCESS_SOFT
            else:
                text, fill = "Click Edit on the group that isn't ready", ui.ROW_BAND
        elif draw["mode"] == "box" and g["roi"] is None:
            text, fill = "GROUP %d — drag a box round its stripes" % n, ui.ROW_BAND
        elif draw["mode"] == "area" and g["area"] is None:
            text, fill = "GROUP %d — drag a box round the cable at its stripes" % n, ui.ROW_BAND
        elif g["model"] is None:
            text, fill = "GROUP %d — %s" % (n, g["error"] or "no stripes found"), ui.DANGER_SOFT
        elif not _ready(g):
            text, fill = "GROUP %d — %d stripes found, %s expected" % (
                n, g["model"]["count"], g["count"]), ui.DANGER_SOFT
        else:
            text, fill = ("GROUP %d — %s found. Press Done, or adjust the %s box."
                          % (n, _what(g), "stripe" if draw["mode"] == "box" else "cable"),
                          ui.SUCCESS_SOFT)
        banner.config(text=text, bg=fill)

    def _show_frame():
        """The taught frame: the box being drawn, everything else dashed and
        labelled, and every group's stripes outlined."""
        if state["frame"] is None or live["on"]:
            return
        view.set_image(state["frame"])
        g = _group()
        editing_key = "roi" if draw["mode"] == "box" else "area"
        view.set_editable(g is not None)
        view.set_accent(BOX_INK if draw["mode"] == "box" else AREA_INK)
        view.set_roi(g[editing_key] if g is not None else None, notify=False)
        frames = []
        for n, other in enumerate(groups, start=1):
            for key, ink, name in (("roi", BOX_INK, "stripes"), ("area", AREA_INK, "cable")):
                if other[key] and not (other is g and key == editing_key):
                    frames.append((other[key], ink, "G%d %s" % (n, name)))
        view.set_frames(frames)
        view.set_outlines([o for other in groups for o in other["outlines"]])
        view.set_hint("Scroll to zoom  ·  right-drag to move")

    def _suggest_area(roi):
        """A first cable box round a stripe box, for the operator to adjust:
        three times its size each way, within the frame."""
        fh, fw = state["frame"].shape[:2]
        cx = roi["x"] + roi["width"] / 2.0
        cy = roi["y"] + roi["height"] / 2.0
        x0, y0 = max(0, int(cx - 1.5 * roi["width"])), max(0, int(cy - 1.5 * roi["height"]))
        x1 = min(fw, int(cx + 1.5 * roi["width"]))
        y1 = min(fh, int(cy + 1.5 * roi["height"]))
        return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}

    def _analyse(g):
        """Read the group inside its boxes, as saving would."""
        g["model"], g["outlines"], g["error"] = None, [], None
        if state["frame"] is None or g["roi"] is None or g["area"] is None:
            return
        try:
            g["model"], g["outlines"] = stripe_check.read_group(
                state["frame"], g["roi"], g["area"], body=g["body"], colour=g["colour"])
        except ValueError as e:
            g["error"] = str(e)
            return
        # Named from what was seen until the operator chooses otherwise
        g["body"] = g["body"] or g["model"]["body"]
        g["colour"] = g["colour"] or g["model"]["colour"]
        g["count"] = g["count"] or g["model"]["count"]

    def _refresh_all():
        _paint_groups()
        _paint_editor()
        _paint_banner()
        _show_frame()
        _refresh_gates()

    def _check_count(*_a):
        g = _group()
        if g is None:
            return
        model = g["model"]
        n = _count()
        if not state["loading"] and model is not None and n is not None and n != g["count"]:
            g["count"] = n
            _set_btn_enabled(btn_done, _ready(g))
            _paint_groups()
            _paint_banner()
            _refresh_gates()
        if model is not None and n is not None and n != model["count"]:
            count_note.config(
                text="%d were found, but the check would expect %d, so every good part "
                     "would fail. Check the boxes and the lighting; stripes that touch "
                     "can't be told apart." % (model["count"], n),
                fg=WARN)
        else:
            count_note.config(text="", fg=TXT_FAINT)

    def _roi_changed(roi, final=True):
        g = _group()
        if g is None:
            return
        g["roi" if draw["mode"] == "box" else "area"] = roi
        if final:
            # Boxed the stripes: offer a cable box round them, to adjust
            if draw["mode"] == "box" and roi is not None and g["area"] is None:
                g["area"] = _suggest_area(roi)
                draw["mode"] = "area"
            # Counted afresh from the new boxes
            g["count"] = None
            _analyse(g)
            _refresh_all()

    def _set_mode(mode):
        draw["mode"] = mode
        _refresh_all()

    def _scroll_to(step_body):
        """Scroll the rail so a step's heading is at the top of it."""
        canvas = rail_scroll.winfo_children()[0]
        win.update_idletasks()
        height = rail.winfo_height()
        if height > 0:
            top = step_body.master.winfo_rooty() - rail.winfo_rooty()
            canvas.yview_moveto(max(0.0, top - 4) / height)

    def _first_unready():
        for i, g in enumerate(groups):
            if not _ready(g):
                return i
        return None

    def _edit_group(i):
        active["i"] = i
        draw["mode"] = "box"
        _refresh_all()
        # The group's controls, not the steps already done
        _scroll_to(s4)

    def _done_group():
        active["i"] = _first_unready()
        draw["mode"] = "box"
        _refresh_all()
        # Back to the list of groups, with Add Group and the next to do
        _scroll_to(s3 if active["i"] is None else s4)

    def _add_group():
        groups.append(_new_group())
        _edit_group(len(groups) - 1)

    def _remove_group(i):
        if len(groups) > 1 and messagebox.askyesno(
                "Remove Group", "Remove group %d?" % (i + 1), parent=win):
            groups.pop(i)
            if active["i"] is not None:
                active["i"] = None if active["i"] == i else \
                    active["i"] - (1 if active["i"] > i else 0)
            _refresh_all()

    def _on_colour(event=None):
        g = _group()
        if g is None:
            return
        g["body"] = body_var.get() or None
        g["colour"] = colour_var.get() or None
        _refresh_all()

    def _set_live(on):
        live["on"] = on and stream["s"] is not None
        if live["on"]:
            view.set_editable(False)
            view.set_hint("")
            # They belong to the captured frame
            view.set_outlines([])
            view.set_frames([])
            view.set_roi(None, notify=False)
            frame_lbl.config(text="LIVE  ·  camera %d" % cam["index"])
        _refresh_all()
        _paint_buttons()

    def _paint_buttons():
        has_cam = stream["s"] is not None
        for b in (btn_capture, btn_live, btn_import):
            b.pack_forget()
        if has_cam:
            btn_capture.pack(side="left")
            btn_live.pack(side="left", padx=(8, 0))
            _set_btn_enabled(btn_live, not live["on"])
            btn_import.pack(side="left", padx=(8, 0))
        else:
            btn_import.pack(side="left")

    def _use_frame(img, label):
        size_was = None if state["frame"] is None else state["frame"].shape[:2]
        state["frame"], state["label"] = img, label
        live["on"] = False
        # Keep the boxes across captures of the same size; on a re-teach
        # start from the groups the part was taught with
        if size_was is not None and size_was != img.shape[:2]:
            groups[:] = [_new_group()]
        reloaded = False
        if (all(g["roi"] is None for g in groups) and previous
                and previous.get("frame_size") == [img.shape[1], img.shape[0]]):
            groups[:] = [_new_group(_as_box(g.get("inspection_region")), _as_box(g.get("roi")),
                                    g.get("body_colour"), g.get("stripe_colour"),
                                    g.get("stripe_count"))
                         for g in stripe_check.groups_of(previous)] or [_new_group()]
            reloaded = True
        for g in groups:
            _analyse(g)
        # A re-teach opens on group 1, so its box can be checked against the
        # new picture; otherwise straight on to the first group still to do
        active["i"] = 0 if reloaded else _first_unready()
        draw["mode"] = "box"
        view.set_image(img, keep_roi=False)
        short = label if len(label) <= 24 else label[:21] + "…"
        frame_lbl.config(text="%s  ·  %d x %d" % (short, img.shape[1], img.shape[0]))
        frame_note.config(text="Captured (%s)" % short, fg=OK_GREEN)
        _set_live(False)
        _scroll_to(s3)

    def _capture():
        s = stream["s"]
        if s is None:
            return
        frame = s.latest() if live["on"] else s.read(timeout=3.0)
        if frame is None:
            messagebox.showwarning("Capture", "No frame from camera 2 yet.", parent=win)
            return
        _use_frame(frame.copy(), "camera %d" % cam["index"])

    def _import():
        path = filedialog.askopenfilename(
            parent=win, title="Select Image of a Good Part",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        if not path:
            return
        img = _read_image(path)
        if img is None:
            messagebox.showerror("Import", "Could not read that image file.", parent=win)
            return
        _use_frame(img, os.path.basename(path))

    def _save():
        pno = ent_pno.get().strip()
        if not all(_gates().values()):
            return
        if not reteach and pno in existing_parts and not messagebox.askyesno(
                "Replace Stripe Check",
                "“%s” already has a stripe check.\n\nReplace it?" % pno, parent=win):
            return
        fh, fw = state["frame"].shape[:2]
        if cam["enabled"] and (fw, fh) != (cam["width"], cam["height"]):
            if not messagebox.askyesno(
                    "Resolution Mismatch",
                    "This image is %d×%d but camera 2 is set to %d×%d.\n\n"
                    "The check will refuse frames of a different size, so this part "
                    "will fail on the line until it is re-taught.\n\nSave anyway?"
                    % (fw, fh, cam["width"], cam["height"]), parent=win):
                return
        try:
            stripe_check.teach(pno, state["frame"], [
                {"roi": g["roi"], "area": g["area"], "body": g["body"],
                 "colour": g["colour"], "count": g["count"]} for g in groups])
        except (ValueError, OSError) as e:
            messagebox.showerror("Save Failed", str(e), parent=win)
            return
        result["saved"] = pno
        _close()

    def _close():
        alive["v"] = False
        if stream["s"] is not None:
            try:
                stream["s"].release()
            except Exception:
                pass
            stream["s"] = None
        try:
            win.grab_release()
        except Exception:
            pass
        win.destroy()

    btn_capture.configure(command=_capture)
    btn_import.configure(command=_import)
    btn_live.configure(command=lambda: _set_live(True))
    btn_save.configure(command=_save)
    btn_cancel.configure(command=_close)
    cmb_body.bind("<<ComboboxSelected>>", _on_colour)
    cmb_colour.bind("<<ComboboxSelected>>", _on_colour)
    count_var.trace_add("write", _check_count)
    win.protocol("WM_DELETE_WINDOW", _close)
    win.bind("<Escape>", lambda e: _close())
    ent_pno.bind("<KeyRelease>", _refresh_gates)

    # ── Camera bring-up ────────────────────────────────────────────────────
    if cam["enabled"]:
        stream["s"] = camera.acquire(cam["index"], cam["width"], cam["height"])
        view.set_placeholder("Starting camera %d…" % cam["index"])
        _set_live(True)
    else:
        view.set_placeholder("Camera 2 is not configured\n\n"
                             "Import a photo of a good part, or set camera 2 up first.")
        _set_live(False)

    def _tick():
        if not alive["v"]:
            return
        s = stream["s"]
        if live["on"] and s is not None:
            frame = s.latest()
            if frame is not None:
                view.set_image(frame)
            elif not s.is_alive():
                live["on"] = False
                view.set_image(None)
                view.set_placeholder("Camera %d stopped responding" % cam["index"])
                frame_lbl.config(text="CAMERA UNAVAILABLE")
        try:
            win.after(60, _tick)
        except Exception:
            pass

    _paint_buttons()
    _paint_zoom()
    _refresh_all()
    _tick()

    parent.wait_window(win)
    return result["saved"]


def _open_stripe_test(parent, part_number):
    """Run the production stripe check on camera 2, or on a still image."""
    from vision_engine.stripes import stripe_check

    win = _dialog(parent, "Stripe Test", 900, 660)
    _dialog_header(win, "Stripe Test — %s" % part_number,
                   "Runs the same check the test cycle uses — on camera 2, or a "
                   "still image you supply.")

    foot_in = _dialog_footer(win)

    verdict = tk.Frame(win, bg=ui.SUBTLE, height=54)
    verdict.pack(fill="x")
    verdict.pack_propagate(False)
    verdict_lbl = tk.Label(verdict, text="RUNNING…", bg=ui.SUBTLE, fg=TXT_DIM,
                           font=(FONT, 22, "bold"))
    verdict_lbl.pack(side="left", padx=18)
    verdict_note = tk.Label(verdict, text="", bg=ui.SUBTLE, fg=TXT_DIM,
                            font=(FONT, 11), anchor="e", justify="right")
    verdict_note.pack(side="right", padx=18)

    body = tk.Frame(win, bg=BG)
    body.pack(fill="both", expand=True, padx=14, pady=12)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(0, weight=1)

    view_wrap = tk.Frame(body, bg=LINE)
    view_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
    view = RoiView(view_wrap, editable=False)
    view.pack(fill="both", expand=True, padx=1, pady=1)
    view.set_placeholder("Capturing…")

    rail = tk.Frame(body, bg=BG, width=260)
    rail.grid(row=0, column=1, sticky="ns")
    rail.pack_propagate(False)

    metrics = _card(rail, "Result", icon="chart")
    metrics.pack(fill="x")
    mb = metrics.body
    m_source = _kv_row(mb, "Source", "Camera 2")
    m_expected = _kv_row(mb, "Expected", "—", mono=True)
    m_found = _kv_row(mb, "Found", "—", mono=True)
    # Several groups make these long, so they wrap rather than run off the card
    for value in (m_expected, m_found):
        value.config(wraplength=140, justify="left")
    m_time = _kv_row(mb, "Time", "—", mono=True)

    hint = tk.Label(rail, text="", bg=BG, fg=TXT_DIM, font=(FONT, 10),
                    wraplength=240, justify="left", anchor="w")
    hint.pack(fill="x", pady=(12, 0))

    btn_close = _btn(foot_in, "Close", BTN_NEUTRAL, font_size=12, pady=8)
    btn_close.pack(side="right")
    btn_rerun = _btn(foot_in, "Run Again", BTN_PRIMARY, font_size=12, pady=8,
                     icon="refresh")
    btn_rerun.pack(side="right", padx=(0, 8))
    btn_source = _btn(foot_in, "Test Image…", BTN_NEUTRAL, font_size=12, pady=8,
                      icon="document")
    btn_source.pack(side="right", padx=(0, 8))

    alive = {"v": True}
    source = {"kind": "camera", "image": None, "label": None}
    busy = {"v": False}
    pending = {"v": False}

    def _paint_verdict(fill):
        verdict.config(bg=fill)
        for w_ in (verdict_lbl, verdict_note):
            w_.config(bg=fill)

    def _run():
        if not alive["v"]:
            return
        if busy["v"]:
            pending["v"] = True
            return
        busy["v"] = True
        verdict_lbl.config(text="RUNNING…", fg=TXT_DIM)
        verdict_note.config(text="")
        _paint_verdict(ui.SUBTLE)
        _set_btn_enabled(btn_rerun, False)

        frame = source["image"] if source["kind"] == "image" else None
        out = {}

        def _work():
            try:
                out["result"] = stripe_check.inspect(part_number, frame=frame)
            except Exception as e:
                out["error"] = e

        worker = threading.Thread(target=_work, daemon=True)
        worker.start()

        def _wait():
            if not alive["v"]:
                return
            if worker.is_alive():
                win.after(80, _wait)
                return
            busy["v"] = False
            if pending["v"]:
                pending["v"] = False
                _run()
                return
            _set_btn_enabled(btn_rerun, True)
            if "error" in out:
                verdict_lbl.config(text="ERROR", fg=WARN)
                verdict_note.config(text=str(out["error"]), fg=WARN)
                _paint_verdict(ui.ROW_BAND)
                return
            _show(out["result"])

        _wait()

    def _show(result):
        color = _VERDICT_INK.get(result.judgement, TXT_DIM)
        _paint_verdict(_VERDICT_FILL.get(result.judgement, ui.SUBTLE))
        verdict_lbl.config(text=result.judgement, fg=color)
        verdict_note.config(text=result.error or "Stripes match", fg=color)

        m_source.config(text="Camera 2" if source["kind"] == "camera" else source["label"])
        model = stripe_check.load_model(part_number)
        # One group to a line
        m_expected.config(text=stripe_check.describe(model).replace(" + ", " +\n")
                          if model else "—", fg=TXT)
        m_found.config(text=result.seen.replace(" x", " × ").replace(" + ", " +\n")
                       if result.seen else "—", fg=color)
        m_time.config(text="%d ms" % result.processing_time_ms, fg=TXT)

        if result.frame is not None:
            view.set_image(result.frame)
            view.set_outlines(result.stripe_boxes)
            view.set_accent(color)
            view.set_roi(None, notify=False)
            # Each group's cable box, in its own verdict's colour
            several = len(result.groups) > 1
            view.set_frames([
                ({"x": g.region[0], "y": g.region[1], "width": g.region[2],
                  "height": g.region[3]},
                 OK_GREEN if g.ok else NG_RED,
                 ("G%d " % n if several else "") + g.seen.replace(" x", " × "))
                for n, g in enumerate(result.groups, start=1) if g.region])
            view.set_hint("Each group was judged in its own cable box")
        else:
            view.set_frames([])
            view.set_image(None)
            view.set_placeholder(result.error or "No frame captured")

        if result.judgement == "NG":
            hint.config(
                text="If this part is good, check the lighting and that camera 2 sees "
                     "it from the same distance as when it was taught, then re-teach it "
                     "if it still fails.",
                fg=WARN)
        elif result.judgement == "ERROR":
            hint.config(text="The picture couldn't be judged reliably — fix the cause "
                             "above and run again. A part taught with the earlier stripe "
                             "check must be re-taught.",
                        fg=WARN)
        else:
            hint.config(text="", fg=TXT_DIM)

    def _pick_image():
        if source["kind"] == "image":
            # Already testing an image — the button toggles back to the camera.
            source["kind"], source["image"], source["label"] = "camera", None, None
            btn_source.configure(text="Test Image…")
            _run()
            return
        path = filedialog.askopenfilename(
            parent=win, title="Select Test Image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        if not path:
            return
        img = _read_image(path)
        if img is None:
            messagebox.showerror("Test Image", "Could not read that image file.", parent=win)
            return
        source["kind"], source["image"], source["label"] = "image", img, os.path.basename(path)
        btn_source.configure(text="Use Camera 2")
        _run()

    def _close():
        alive["v"] = False
        try:
            win.grab_release()
        except Exception:
            pass
        win.destroy()

    btn_rerun.configure(command=_run)
    btn_source.configure(command=_pick_image)
    btn_close.configure(command=_close)
    win.protocol("WM_DELETE_WINDOW", _close)
    win.bind("<Escape>", lambda e: _close())

    _run()
    parent.wait_window(win)


def main():
    root = tk.Tk()
    root.state("zoomed")
    VisionSettings(root)
    root.mainloop()


if __name__ == "__main__":
    main()
