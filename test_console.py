import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.font as tkfont
from PIL import Image, ImageTk
import customtkinter as ctk
import os
import mysql.connector
import threading
import json
from pymodbus.client import ModbusSerialClient, ModbusTcpClient  # PLC functionality restored
import serial
from serial.tools import list_ports  # Proper import for port enumeration
import time
import traceback
from datetime import datetime
import sys
import queue
import math

import config
import data_files
import db
import ui
from plc_address import bit_address

# Camera 2 checks the stripes on the cable, and both cameras' pictures are
# kept with each test. Without OpenCV the console still runs, camera 2 shows
# OFF and no pictures are kept.
try:
    import numpy as np
    from vision_engine import camera, captures, stripe_check
    from vision_engine import (VisionController, load_camera_config, load_vision_config,
                               save_vision_config)
except ImportError:
    camera = captures = stripe_check = None


class SerializedModbusClient:
    """A Modbus client that lets one request through at a time.

    The status worker thread and the Tk thread both talk to the PLC. On a
    serial line two requests in flight at once garble each other's frames,
    so every call on the client goes through one lock.
    """

    def __init__(self, client):
        self._client = client
        self._lock = threading.RLock()

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            with self._lock:
                return attr(*args, **kwargs)
        return call


def send_raw_to_printer(printer_name, data):
    """Send bytes to a Windows printer untranslated.

    Label templates are printer command language, so they must reach the
    printer as-is rather than through a driver's page rendering.
    """
    import ctypes
    from ctypes import wintypes

    class DOC_INFO_1(ctypes.Structure):
        _fields_ = [("pDocName", wintypes.LPWSTR),
                    ("pOutputFile", wintypes.LPWSTR),
                    ("pDatatype", wintypes.LPWSTR)]

    winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
    handle = wintypes.HANDLE()
    if not winspool.OpenPrinterW(printer_name, ctypes.byref(handle), None):
        raise OSError(f"Printer '{printer_name}' not found (error {ctypes.get_last_error()})")
    try:
        doc = DOC_INFO_1("EOL BARCODE LABEL", None, "RAW")
        if not winspool.StartDocPrinterW(handle, 1, ctypes.byref(doc)):
            raise OSError(f"Could not start print job (error {ctypes.get_last_error()})")
        try:
            winspool.StartPagePrinter(handle)
            written = wintypes.DWORD()
            ok = winspool.WritePrinter(handle, data, len(data), ctypes.byref(written))
            winspool.EndPagePrinter(handle)
            if not ok or written.value != len(data):
                raise OSError(f"Printer accepted {written.value} of {len(data)} bytes "
                              f"(error {ctypes.get_last_error()})")
        finally:
            winspool.EndDocPrinter(handle)
    finally:
        winspool.ClosePrinter(handle)


class LiveCameraView(tk.Frame):
    """One camera's live picture, with its verdict in a strip underneath.

    While a part is tested the view shows the camera's stream. Once a check
    has judged a frame, hold() keeps that frame on screen until resume(), so
    the operator can see what was judged and why.
    """

    REFRESH_MS = 80

    def __init__(self, parent, height, font, idle_fill, on_click=None):
        super().__init__(parent, bg='black')
        box = tk.Frame(self, bg='black', height=height)
        box.pack(fill="x")
        box.pack_propagate(False)
        self.picture = tk.Label(box, bg='black', fg='#A0A0A0', font=(font, 10, 'bold'))
        self.picture.pack(fill="both", expand=True)
        self.strip = tk.Label(self, text="STATUS", bg=idle_fill, fg='black',
                              font=(font, 12, 'bold'))
        self.strip.pack(fill="x")
        if on_click:
            # Click the picture to choose which camera feeds it
            self.picture.configure(cursor="hand2")
            self.picture.bind("<Button-1>", lambda e: on_click())
        self.stream = None
        self.held = None
        self.photo = None
        self.note = "Camera not set up"
        self.after(self.REFRESH_MS, self.refresh)

    def set_status(self, text, fill):
        self.strip.configure(text=text, bg=fill)

    def set_stream(self, stream, note="Camera not set up"):
        """Show this camera stream; with None, show `note` instead."""
        self.stream = stream
        self.note = note

    def hold(self, frame):
        """Keep showing `frame`, the one a check judged, until resume()."""
        if frame is not None:
            self.held = frame
            self.show(frame)

    def resume(self):
        self.held = None

    def refresh(self):
        if not self.winfo_exists():
            return
        if self.held is None:
            frame = self.stream.latest() if self.stream is not None else None
            if frame is not None:
                self.show(frame)
            elif self.stream is None or not self.stream.is_alive():
                self.photo = None
                self.picture.configure(
                    image="", text=self.note if self.stream is None else "Camera not responding")
        self.after(self.REFRESH_MS, self.refresh)

    def show(self, frame):
        """Fit a BGR frame into the box, keeping its shape."""
        w, h = self.picture.winfo_width(), self.picture.winfo_height()
        if w < 10 or h < 10:
            return
        scale = min(w / frame.shape[1], h / frame.shape[0])
        size = (max(1, int(frame.shape[1] * scale)), max(1, int(frame.shape[0] * scale)))
        rgb = Image.fromarray(frame[..., ::-1].copy())     # BGR to RGB
        self.photo = ImageTk.PhotoImage(rgb.resize(size, Image.Resampling.BILINEAR))
        self.picture.configure(image=self.photo, text="")


class FooterMessage(tk.Label):
    """The machine status line in the footer.

    show(message, colour) takes the colour words the console passes. While
    there is a message it blinks, so it catches the eye from across the cell.
    """

    INK = {'green': '#008000', 'red': '#FF0000', 'orange': '#FF8C00',
           'blue': '#0000FF', 'black': '#000000'}
    SHOWN_MS = 800
    HIDDEN_MS = 600

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self._message = ''
        self._visible = True
        self._blink_job = None

    def show(self, message, color='blue'):
        ink = self.INK.get(str(color).lower(), color if str(color).startswith('#') else '#0000FF')
        self._message = message
        self._visible = True
        self.configure(text=message, fg=ink)
        if self._blink_job is not None:
            self.after_cancel(self._blink_job)
            self._blink_job = None
        if message:
            self._blink_job = self.after(self.SHOWN_MS, self._blink)

    def _blink(self):
        try:
            self._visible = not self._visible
            self.configure(text=self._message if self._visible else '')
            delay = self.SHOWN_MS if self._visible else self.HIDDEN_MS
            self._blink_job = self.after(delay, self._blink)
        except tk.TclError:
            self._blink_job = None  # the window has closed


class EOLTesterGUI:
    def __init__(self, root):
        self.root = root
        ui.apply(root)
        
        # Get machine ID from environment
        machine_id = config.get('MACHINE_ID', '')
        
        # Set title with machine ID
        title = "EOL (END OF LINE) TESTER"
        if machine_id:
            title += f" - Machine ID: {machine_id}"
        self.root.title(title)
        
        # Initialize variables before setting up the window
        self.initialize_variables()
        
        # Set up the window after initialization
        self.root.after(100, self.setup_window)  # Delay window setup slightly
        
        # Set up window closing handler
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # Continue with the rest of your initialization...

    def ensure_settings_file_exists(self):
        """Make sure the settings file exists; config.py creates it on demand."""
        config.reload()
        return config.CONFIG_FILE

    def load_plc_config(self):
        """Load PLC configuration from the settings file"""
        try:
            # PLC connection settings
            self.plc_com_port = config.get('PLC_COM_PORT', 'COM5').strip("'")
            self.plc_baud_rate = int(config.get('PLC_BAUD_RATE', '38400'))
            self.plc_station_id = int(config.get('PLC_STATION_ID', '1'))
            
            # TCP settings (if available)
            self.plc_tcp_ip = config.get('MODBUS_TCP_IP', '').strip("'")
            self.plc_tcp_port = int(config.get('MODBUS_TCP_PORT', '502')) if config.get('MODBUS_TCP_PORT') else 502
            
            # Register settings
            self.plc_reg_address = config.get('PLC_REG_ADDRESS', '').strip("'")
            self.plc_points_to_read = int(config.get('PLC_POINTS_TO_READ', '1'))

            # Per-request timeout and retries. Keep them short: every PLC
            # request waits its turn, so one the PLC never answers holds up
            # all the others, including the window's own.
            self.plc_timeout = config.get_int('PLC_READ_TIMEOUT', 500) / 1000.0
            self.plc_retries = config.get_int('PLC_RETRIES', 0)
            
            print(f"PLC Config loaded - COM: {self.plc_com_port}, Baud: {self.plc_baud_rate}, Station: {self.plc_station_id}")
            if self.plc_tcp_ip:
                print(f"PLC TCP Config - IP: {self.plc_tcp_ip}, Port: {self.plc_tcp_port}")
                
        except Exception as e:
            print(f"Error loading PLC configuration: {e}")
            # Set defaults
            self.plc_com_port = 'COM5'
            self.plc_baud_rate = 38400
            self.plc_station_id = 1
            self.plc_tcp_ip = ''
            self.plc_tcp_port = 502
            self.plc_timeout = 0.5
            self.plc_retries = 0

    def initialize_variables(self):
        """Initialize all variables before window setup and ensure clean COM ports"""
        # Initialize UI-related variables
        self.image_label = None
        self.current_image = None
        self.label_positions = {}
        self.selected_label = None
        self.barcode_data = ""
        self.label_widgets = {}
        
        # Initialize container references (will be properly created in setup_window)
        self.main_container = None
        self.message_label = None
        self.workspace = None
        
        # Thread-safe queue for PLC status updates
        self.plc_status_queue = queue.Queue(maxsize=10)
        self.plc_read_thread = None
        self.plc_thread_running = False
        
        # Initialize PLC connection status
        self.plc_connected = False
        self.plc_client = None
        
        # Hold register addresses for loadcell/pressure data
        self.hold_register_addresses = []
        self.latest_register_data = {}  # Store latest register readings
        
        # Process step tracking - ensure all 8 steps are visited
        self.visited_steps = set()  # Track which steps have been visited
        self.step_visit_times = {}  # Track when each step was visited
        self.required_steps = ['M0067', 'M0068', 'M0076', 'M0085', 'M0078', 'M0087', 'M0075', 'M0079']
        self.last_active_step = None  # Track last active step for transition detection
        
        # P0000 keep-alive to prevent PLC from stopping
        self.p0000_keepalive_active = False
        self.p0000_last_write = 0
        
        # Dynamic monitoring - adjust speed based on PLC activity
        self.monitoring_interval = 3000  # Start with 3 seconds
        self.last_status_values = {}  # Track last status to detect changes
        
        # Make sure the settings file exists and is freshly read
        self.ensure_settings_file_exists()
        
        # Load PLC configuration from the settings file
        self.load_plc_config()
        
        # Get machine ID from the settings file and store it
        self.machineid = config.get('MACHINE_ID', 'Not Set')
        
        # Initialize arrays for different data types
        self.process_status_array = []
        self.program_selection_array = []
        self.input_sensors_array = []
        self.employee_codes = []
        
        # Employee validation system
        self.current_employee_id = None
        self.employee_validation_complete = False
        self.authorized_employee_codes = []
        self.current_part_number = None
        self.current_lot_number = None
        self.specifications = {}
        
        # Initialize additional variables  
        self.keepWriting = False
        self.breakLoop = False
        
        # Initialize loadcell clients
        self.loadcell1_client = None
        self.loadcell2_client = None
        
        # Initialize test flow variables
        self.rcvdTestRslt = False
        self.awaiting_result_clear = False
        self.plc_status_loop_running = False
        # Why the last coil write failed, for the START button to report
        self.last_plc_error = ""
        # Most recent coil snapshot from the worker thread, for code on the
        # Tk thread that must not wait on the PLC itself
        self.latest_plc_status = {}

        # Labels on the part image and the sensors behind them. The worker
        # thread reads sensor_label_keys and writes sensor_states; the Tk
        # thread owns placed_labels.
        self.placed_labels = {}
        self.sensor_label_keys = []
        self.sensor_states = {}
        self.input_sensor_addresses = []

        # The printed label waiting to be scanned, if any
        self.current_alc_code = ""
        self.awaiting_label_scan = False
        self.label_scan_code = ""
        self.loadcell01Value = 0.0
        self.loadcell02Value = 0.0
        self.loadcell03Value = 0.0
        self.loadcell04Value = 0.0
        self.L1MaxValue = 0.0
        self.L2MaxValue = 0.0
        self.L3MaxValue = 0.0
        self.L4MaxValue = 0.0
        self.P01Value = 0.0
        self.P02Value = 0.0
        self.P03Value = 0.0
        self.P04Value = 0.0
        self.failCounter = 0
        self.passCounter = 0
        self.columnL2 = False
        self.columnL3 = False
        self.columnL4 = False
        self.columnP3 = False
        self.columnP4 = False
        self.cam1Result = ""
        self.cam2Result = ""
        self.deviceToRead = []
        self.inputSensorsToReadList = []
        self.mldDataTable = []
        
        # Database configuration for test flow
        self.db_config = db.get_config()
        
        # Part information variables
        self.partNumber = ""
        self.modelName = ""
        self.vendorCode = ""
        self.eoNumber = ""
        self.specialData = ""
        self.initialID = ""
        self.supplierSection = ""
        self.lotNo = ""
        self.traceabilityCode = ""
        self.today = datetime.today().date()
        self.dataPointX = 0

        # Plotted chart points, keyed by series name
        self.chart_series = {name: [] for name in
                             ('L1', 'L2', 'L3', 'L4', 'P1', 'P2', 'P3', 'P4')}
        # Today's OK cables for the loaded part, keyed by hour of the day
        self.uph_counts = {}

        self.barcodePrintFileName = ""
        self.prnFileContent = ""
        self.programSelectionPLCAddress = ""
        self.machineOnPLCCoilAddress = ""
        self.alertOnPLCCoilAddress = ""
        self.startButtonPLCCoilAddress = self.START_BUTTON_ADDRESS
        self.partRunningSerialExists = False
        
        # NG Cable validation flags
        self.startingNGCableValidation = False
        self.endingNGCableValidation = False
        self.endingNGCableValidated = False
        
        # Barcode scanning variables
        self.printedLabelScanDataInput_Received = False
        self.alcInput_TimeInterval = 3000
        self.printedLabelScanDataInput_TimeInterval = 4000
        self.printedLabelScanDataInput_WaitTime = 6000
        self.alertOn_TimeInterval = 5000
        
        # EOL Testing Variables
        self.alcInput_TimeInterval = 3000  # 3000ms for manual entry
        # Label scan timings come from the Testing section of .config
        self.printedLabelScanDataInput_TimeInterval = config.get_int('PRINTED_LABEL_SCAN_TIME_INTERVAL', 4000)
        self.printedLabelScanDataInput_WaitTime = config.get_int('PRINTED_LABEL_SCAN_WAIT_TIME', 6000)
        self.alertOn_TimeInterval = config.get_int('ALERT_ON_TIME_INTERVAL', 5000)
        self.alertOn_TimeInterval = 5000  # 5 secs alert duration
        self.printedLabelScanDataInput_Received = False
        
        # Part Information Variables
        self.barcodePrintFileName = ""
        self.barcodePrintFileNamePath = ""
        self.prnFileContent = ""
        self.partNumber = ""
        self.modelName = ""
        self.vendorCode = ""
        self.eoNumber = ""
        self.specialData = ""
        self.initialID = ""
        self.supplierSection = ""
        self.lotNo = ""
        self.traceabilityCode = ""
        self.today = datetime.today().date()
        self.dataPointX = 0
        self.chart_series = {name: [] for name in
                             ('L1', 'L2', 'L3', 'L4', 'P1', 'P2', 'P3', 'P4')}

        # The current lot panel: the part it shows (None once released), the
        # part's OPEN row in TBL_PART_LOT, and the size the operator set
        self.lot_part = None
        self.open_lot = None
        self.lot_size = None
        self.lot_total_ok = 0
        self.lot_number = 0

        # Machine and Process Variables
        self.machineID = self.machineid  # Use existing machine ID
        self.startingNGCableValidation = False
        self.endingNGCableValidated = False
        self.endingNGCableValidation = False
        self.deviceToRead = []
        
        # Load Cell Values
        self.loadcell01Value = 0.0
        self.loadcell02Value = 0.0
        self.loadcell03Value = 0.0
        self.loadcell04Value = 0.0
        
        # Maximum Values During Test
        self.L1MaxValue = 0.0
        self.L2MaxValue = 0.0
        self.L3MaxValue = 0.0
        self.L4MaxValue = 0.0
        
        # Pressure Values
        self.P01Value = 0.0
        self.P02Value = 0.0
        self.P03Value = 0.0
        self.P04Value = 0.0
        
        # Test Control Variables
        self.failCounter = 0
        self.passCounter = 0
        self.blink = False
        
        # Column Visibility Flags
        self.columnL2 = False
        self.columnL3 = False
        self.columnL4 = False
        self.columnP3 = False
        self.columnP4 = False
        
        # PLC Communication Variables
        self.slaveAddress = 1
        self.inputSensorsArray = []
        self.processStatusArray = []
        self.dataRegistersArray = []
        self.employeeCodesArray = []
        self.processStatusLabels = ["AUTO", "HOME", "PULL1_OK", "PULL1_NG", "PULL2_OK", "PULL2_NG", "TESTRESULT_OK", "TESTRESULT_NG"]
        self.rcvdTestRslt = False
        self.inputSensorsToReadList = []
        self.programSelectionPLCAddress = ""
        
        # Camera and Alert Variables
        self.cam1Result = ""
        self.cam2Result = ""
        self.cam1Image = None       # pictures of this test, relative paths
        self.cam2Image = None
        self.camera_streams = {}    # by camera source, e.g. "cam2"
        self.camera_check_running = False
        self.camera_checks_done = False     # this test's checks are in
        self.after_camera_checks_do = None  # waiting for them, to save the part
        self.cam1_by_app = False            # camera 1 judged here, not by the PLC
        self.resetPLCOnFormClosing = False
        self.machineOnPLCCoilAddress = ""
        self.alertOnPLCCoilAddress = ""
        self.partRunningSerialExists = False
        
        # Monitoring and Threading
        self.monitoring_active = False
        self.test_in_progress = False
        self.employee_validated = False
        self.alc_validated = False
        
        # Specification Data
        self.mldDataTable = []
        self.specificationData = []
        self.failCounter = 0
        self.startingNGCableValidation = False
        self.endingNGCableValidation = False
        self.noOfValues = 0
        self.process_status_index = 0  # Initialize process status index for tracking cycle position
        
        # Initialize blinking jobs tracking
        self.blinking_jobs = {}
        
        # Initialize timers
        self.alc_timer = None
        self.alcInput_TimeInterval = 200  # milliseconds
        
        # Track test result states to prevent duplicate saves
        self.last_test_result_pass_state = False
        self.last_test_result_ng_state = False
        self.test_result_saved = False
        
        # Initialize continuous loop control variables
        self.continuous_loop_active = False
        self.current_cycle_number = 0
        self.loop_start_time = None
        self.cycle_start_time = None
        self.auto_cycle_delay = 3.0  # Seconds between cycles
        self.max_auto_cycles = 50   # Safety limit for auto cycling
        self.cycle_completion_detected = False
        self.loop_mode = "MANUAL"   # "MANUAL" or "MONITOR" 
        self.step_duration = 3.0    # Duration for each step in manual mode

    # ------------------------------------------------------------------
    # Layout
    #
    # The Test console is drawn to a fixed scheme - pink title and footer
    # bars, a navy part header, sky-blue step lamps, black trend charts -
    # that operators know from the line. Everything under main_container is
    # built with exact_colors, so it keeps these colours rather than taking
    # the dark theme the other pages use.
    # ------------------------------------------------------------------

    # The shared palette (ui.py) is this console's; these are its names here
    FONT = ui.FONT_FAMILY
    TITLE_PINK = ui.TITLE_PINK
    FOOTER_PINK = ui.FOOTER_PINK
    PANEL = ui.APP_BG           # window grey behind the panels
    NAVY = ui.NAVY              # part header
    SKY = ui.SKY                # an idle step lamp
    AQUA = ui.AQUA
    YELLOW = ui.YELLOW
    SILVER = ui.SILVER
    GREEN = ui.LOT_GREEN        # lot progress and scan counters
    EDGE = ui.BORDER            # thin panel borders
    PLACEHOLDER_INK = '#A9A9A9'
    EMP_PLACEHOLDER = 'EMPLOYEE CODE'
    ALC_PLACEHOLDER = 'ALC CODE'
    SCAN_PLACEHOLDER = 'PRINTED LABEL SCAN DATA'
    LABEL_COUNT = 16            # labels L1-L16, as Model Settings offers
    IMAGE_WIDTH = 750           # the canvas Model Settings places labels on
    IMAGE_HEIGHT = 450
    # Height of each camera's live picture, at full screen scale
    LIVE_HEIGHT = 120
    # Screen height taken by everything but the part image: title, footer,
    # part header, lamps, window chrome, and the least the charts and results
    # grid below can work with. On a 1080 screen this leaves the image about
    # the height the specification grid and cameras beside it need, so the
    # three charts get the rest.
    CHROME_HEIGHT = 760

    def setup_window(self):
        """Set up the window after initialization"""
        # Change from fullscreen to maximized state
        self.root.state('zoomed')  # Replace fullscreen with maximized state
        self.root.lift()  # Bring window to front
        self.root.focus_force()  # Force focus

        # Everything the console draws lives in here, in its own colours
        self.main_container = tk.Frame(self.root, bg=self.PANEL, exact_colors=True)
        self.main_container.pack(fill="both", expand=True)

        # Make sure the settings file exists and is freshly read
        self.ensure_settings_file_exists()

        # Load data after the settings are loaded
        self.load_configuration_data()

        # Set up GUI components before connecting to devices
        self.setup_gui()
        self.setup_barcode_listener()

        # Connect to devices after GUI is set up
        self.connect_to_devices()

        # Connect to PLC after GUI setup
        self.root.after(2000, self.connect_to_plc)

        # Initialize employee validation after GUI setup
        self.root.after(1000, self.initialize_employee_validation)

        # Ensure window stays on top during initialization
        self.root.after(500, lambda: self.root.attributes('-topmost', False))

    def setup_gui(self):
        self.configure_grid_styles()
        self.create_title_bar()
        # The footer is packed before the workspace so the workspace takes
        # whatever height is left between the two bars.
        self.create_footer()

        self.workspace = tk.Frame(self.main_container, bg=self.PANEL)
        self.workspace.pack(fill="both", expand=True, padx=3, pady=(3, 0))
        self.create_quadrants()

    def configure_grid_styles(self):
        """White grids with a sky-blue header row, for the spec and results tables."""
        style = ttk.Style()
        for name, row_font, row_height in (("Spec.Eol.Treeview", (self.FONT, 13), 30),
                                           ("Data.Eol.Treeview", (self.FONT, 10), 22)):
            style.configure(name, background='white', fieldbackground='white',
                            foreground='black', font=row_font, rowheight=row_height,
                            borderwidth=1, bordercolor=self.EDGE)
            style.map(name, background=[('selected', '#CCE8FF')],
                      foreground=[('selected', 'black')])
            style.configure(f"{name}.Heading", background=self.SKY, foreground='black',
                            font=(self.FONT, 11, 'bold'), relief='flat', padding=(4, 6))
            style.map(f"{name}.Heading", background=[('active', '#33CCFF')])

    START_RIM = '#004D00'       # the START button's edge
    START_PRESSED = '#005000'   # its face while held down

    def create_title_bar(self):
        bar = ui.title_bar(self.main_container, "EOL (END OF LINE) TESTER")

        # Starts a PLC test cycle on the loaded part, beside the machine ID: a
        # rounded green button with a play mark and a darker rim, the shade
        # deepening under the pointer and while it is held down
        self.start_button = ctk.CTkButton(
            bar, text="START", command=self.start_test_click,
            image=ui.icon_image('play', ui.TEXT_ON_DARK, 26), compound='left',
            width=150, height=40, corner_radius=10, border_width=2,
            fg_color=ui.SUCCESS, hover_color=ui.SUCCESS_HOVER,
            border_color=self.START_RIM, bg_color=bar.cget('bg'),
            text_color=ui.TEXT_ON_DARK, font=(self.FONT, 21, 'bold'), cursor='hand2')
        self.start_button.pack(side='right', padx=(0, 10), pady=4)
        # Back to green on a timer rather than on release, which a pop-up
        # opened by the press would take
        def pressed(event):
            self.start_button.configure(fg_color=self.START_PRESSED)
            self.root.after(180, lambda: self.start_button.configure(fg_color=ui.SUCCESS))
        self.start_button.bind('<ButtonPress-1>', pressed, add='+')

        # Whether each camera checks the part is set in its own setup,
        # opened by clicking its live view; see configure_camera()
        self.camera_checks = {number: self.camera_check_saved(number) for number in (1, 2)}

    # Where each camera's on/off choice is kept in the vision settings
    CAMERA_CHECK_KEYS = {1: "vision_enabled", 2: "cam2_enabled"}

    @classmethod
    def camera_check_saved(cls, number):
        if stripe_check is None:
            return False
        cfg = load_vision_config()
        # Before the cameras had a box each, one setting turned both on or
        # off; camera 2 follows it until it has a choice of its own
        default = cfg.get("vision_enabled", True)
        return bool(cfg.get(cls.CAMERA_CHECK_KEYS[number], default))

    def camera_check_on(self, number):
        """Whether camera `number` checks the part in the next test started."""
        return stripe_check is not None and self.camera_checks.get(number, False)

    def allow_camera_check_change(self, number, window):
        """Ask for a login before a camera is turned on or off in its setup.

        Turning a camera off lets parts pass without it, so the change needs
        the same login as Settings and Vision Settings.
        """
        from login_form import prompt_login

        user = prompt_login(window)
        if user is None:
            messagebox.showwarning("Camera Check",
                                   f"Camera {number} check not changed - a login is needed.",
                                   parent=window)
            return False
        self.camera_check_user = user
        return True

    def ask_lot_size(self):
        """Ask for the loaded part's lot size in a pop-up. True once one is set.

        The size goes through the lot box, so it is checked and saved just
        as one typed there would be.
        """
        dialog = tk.Toplevel(self.root, bg='white')
        dialog.title("Lot Size")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        tk.Label(dialog, text="ENTER LOT SIZE", bg=self.NAVY, fg='white',
                 font=(self.FONT, 16, 'bold'), pady=10).pack(fill="x")
        body = tk.Frame(dialog, bg='white')
        body.pack(fill="both", expand=True, padx=24, pady=(16, 8))
        tk.Label(body, text=f"Part {self.current_part_number} has no lot size.\n"
                            "Enter how many OK cables make one lot to start the test.",
                 bg='white', fg='black', font=(self.FONT, 12), justify="center").pack()
        entry = tk.Entry(body, width=8, font=(self.FONT, 22, 'bold'), justify="center",
                         bg=self.YELLOW, fg='black', insertbackground='black',
                         relief="solid", bd=1)
        entry.pack(pady=(14, 4), ipady=4)
        problem = tk.Label(body, text="", bg='white', fg=ui.DANGER, font=(self.FONT, 11, 'bold'))
        problem.pack()

        buttons = tk.Frame(dialog, bg='white')
        buttons.pack(fill="x", padx=24, pady=(4, 18))
        buttons.grid_columnconfigure(0, weight=1, uniform='button')
        buttons.grid_columnconfigure(1, weight=1, uniform='button')

        def start():
            text = entry.get().strip()
            if not text.isdigit() or int(text) <= 0:
                problem.configure(text="Enter a whole number above 0")
                entry.focus_set()
                return
            box = self.lot_size_entry
            box.delete(0, tk.END)
            box.insert(0, text)
            self.apply_lot_size()
            if self.lot_size == int(text):
                dialog.destroy()
            else:
                problem.configure(text="The lot size could not be saved")

        tk.Button(buttons, text="START TEST", bg=ui.SUCCESS, fg='white',
                  activebackground=ui.SUCCESS_HOVER, activeforeground='white',
                  font=(self.FONT, 13, 'bold'), relief='raised', bd=1, cursor='hand2',
                  command=start).grid(row=0, column=0, sticky="ew", padx=(0, 6), ipady=4)
        tk.Button(buttons, text="CANCEL", bg=ui.SUBTLE, fg='black',
                  font=(self.FONT, 13, 'bold'), relief='raised', bd=1, cursor='hand2',
                  command=dialog.destroy).grid(row=0, column=1, sticky="ew", padx=(6, 0),
                                               ipady=4)
        entry.bind("<Return>", lambda e: start())
        dialog.bind("<Escape>", lambda e: dialog.destroy())

        # Centred over the console, holding the keyboard until it closes
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_reqheight()) // 3
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dialog.grab_set()
        entry.focus_set()
        self.root.wait_window(dialog)
        self.root.focus_set()
        return bool(self.lot_size)

    def start_test_click(self):
        """Start a PLC test cycle on the loaded part.

        Writes the part's program-select coil and Machine On, then watches
        the PLC as for any cycle, so the result is scored and saved as usual.
        """
        if not self.employee_validation_complete:
            messagebox.showwarning("Start Test", "Enter your employee code first.")
            return
        if not getattr(self, 'current_part_number', None):
            messagebox.showwarning("Start Test", "Scan an ALC code to load a part first.")
            return
        # The OK cables are counted into lots, so a test needs a lot size;
        # one entered here starts the test straight away
        if not self.lot_size and not self.ask_lot_size():
            self.safe_update_message("Enter the lot size, then press START", "red")
            return
        try:
            connected = bool(self.plc_client) and self.plc_client.is_socket_open()
        except Exception:
            connected = False
        if not connected:
            messagebox.showerror("Start Test", "The PLC is not connected.")
            return
        # Before the PLC is told anything, so an aborted test never starts
        if not self.cameras_ready_for_test():
            return

        self.trace(f"START pressed for part {self.current_part_number}")
        # Each failed write says which coil it tried and what the PLC answered
        not_m_coil = "address missing or not an M coil (e.g. M1000)"
        failed = []
        self.last_plc_error = not_m_coil
        if not self.write_program_selection_to_plc():
            failed.append(f"Program Select {self.programSelectionPLCAddress or '(not set)'}"
                          f" - {self.last_plc_error}")
        self.last_plc_error = not_m_coil
        if not self.write_machine_on_to_plc():
            failed.append(f"Machine On {self.machineOnPLCCoilAddress or '(not set)'}"
                          f" - {self.last_plc_error}")
        # Pressed only once the rest is in place, so the PLC starts on this part
        if not failed:
            self.last_plc_error = not_m_coil
            if not self.press_start_button_on_plc():
                failed.append(f"Start {self.startButtonPLCCoilAddress or '(not set)'}"
                              f" - {self.last_plc_error}")
        if failed:
            messagebox.showerror(
                "Start Test",
                "The test was not started. Could not write:\n\n"
                + "\n".join(f"  - {item}" for item in failed))
            return

        self.log_operator_action("TEST_START", "Started from the START button")
        self.safe_update_message("Test started - waiting for the PLC...", "blue")
        self.start_check_async()

    def create_footer(self):
        bar = tk.Frame(self.main_container, bg=self.FOOTER_PINK, height=34)
        bar.pack(fill="x", side="bottom", padx=3, pady=3)
        bar.pack_propagate(False)

        tk.Label(bar, text="Powered By: NICE COMPUTERS AND SOFTWARE SOLUTIONS, Kavali, A.P",
                 bg=self.FOOTER_PINK, fg='black',
                 font=(self.FONT, 11, 'bold')).pack(side="left", padx=8)
        tk.Label(bar, text="*** Scan Result denotes Scanner NOT IN USE or Invalid/Unreadable Barcode Label",
                 bg=self.FOOTER_PINK, fg='black',
                 font=(self.FONT, 11, 'bold')).pack(side="right", padx=8)

        # The machine status line, between the two
        self.message_label = FooterMessage(bar, bg=self.FOOTER_PINK,
                                           font=(self.FONT, 14, 'bold'))
        self.message_label.pack(side="left", fill="x", expand=True)

    def create_quadrants(self):
        # Part and charts on the left, specs and results on the right; the
        # top row sizes to the part image, the bottom row takes the rest.
        self.workspace.grid_columnconfigure(0, weight=56, uniform='column')
        self.workspace.grid_columnconfigure(1, weight=44, uniform='column')
        self.workspace.grid_rowconfigure(0, weight=0)
        self.workspace.grid_rowconfigure(1, weight=1)

        self.q1 = self.create_first_quadrant()
        self.q2 = self.create_second_quadrant()
        self.q3 = self.create_third_quadrant()
        self.q4 = self.create_fourth_quadrant()

        self.q1.grid(row=0, column=0, sticky="nsew", padx=(0, 2), pady=(0, 2))
        self.q2.grid(row=0, column=1, sticky="nsew", padx=(2, 0), pady=(0, 2))
        self.q3.grid(row=1, column=0, sticky="nsew", padx=(0, 2), pady=(2, 0))
        self.q4.grid(row=1, column=1, sticky="nsew", padx=(2, 0), pady=(2, 0))

    def create_first_quadrant(self):
        """Part header, part image with its labels, and the five step lamps."""
        panel = tk.Frame(self.workspace, bg=self.PANEL)

        self.model_header = tk.Label(panel, text="MODEL NAME / PART NAME - PART NUMBER",
                                     bg=self.NAVY, fg='white', pady=6,
                                     font=(self.FONT, 18, 'bold'))
        self.model_header.pack(fill="x")

        picture = tk.Frame(panel, bg='white', highlightthickness=1,
                           highlightbackground=self.EDGE)
        picture.pack(fill="x")

        # Model Settings places labels on a 750x450 canvas. This one is that
        # size scaled to the screen, and label positions scale with it.
        self.image_scale = self.pick_image_scale()
        self.image_frame = tk.Frame(picture, bg='white',
                                    width=round(self.IMAGE_WIDTH * self.image_scale),
                                    height=round(self.IMAGE_HEIGHT * self.image_scale))
        self.image_frame.pack(pady=4)
        self.image_frame.pack_propagate(False)
        self.show_image_placeholder()

        # Names for the labels a part places on the image, from partlabels.txt.
        # Labels the part does not use are not shown.
        self.label_names, label_problem = data_files.part_label_names()
        if label_problem:
            print(f"Warning: {label_problem}; the labels show L1-L{self.LABEL_COUNT}")

        # The lamps stretch down to the bottom of the row, so any height the
        # specification side needs beyond the image goes to them, not a gap
        lamps = tk.Frame(panel, bg=self.PANEL)
        lamps.pack(fill="both", expand=True, pady=(4, 0))
        lamps.grid_rowconfigure(0, weight=1)
        self.process_status_labels = {}
        steps = (('auto', 'AUTO'), ('home', 'HOME'),
                 ('1st', '1st PULL\n(Load Test)'), ('2nd', '2nd PULL\n(Length Test)'),
                 ('test', 'TEST\nRESULT'))
        for column, (key, text) in enumerate(steps):
            lamps.grid_columnconfigure(column, weight=1, uniform='lamp')
            lamp = tk.Label(lamps, text=text, bg=self.SKY, fg='black', height=2,
                            font=(self.FONT, 16, 'bold'))
            lamp.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
            self.process_status_labels[key] = lamp
            # The monitoring loop addresses them as auto_label, home_label ...
            setattr(self, f"{key}_label", lamp)

        return panel

    def create_second_quadrant(self):
        """Specification grid over the camera and printed-label scan boxes."""
        panel = tk.Frame(self.workspace, bg=self.PANEL)

        # Cameras first, packed to the bottom, so the grid takes the rest
        cameras = tk.Frame(panel, bg=self.PANEL)
        cameras.pack(fill="x", side="bottom", pady=(4, 0))
        cameras.grid_columnconfigure(0, weight=1, uniform='camera')
        cameras.grid_columnconfigure(1, weight=1, uniform='camera')
        cameras.grid_columnconfigure(2, weight=2, uniform='camera')

        def group(column, title):
            box = tk.LabelFrame(cameras, text=title, bg=self.PANEL, fg='black',
                                font=(self.FONT, 9, 'bold'), bd=1, relief='groove')
            box.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 4, 0))
            return box

        # Each camera live, its verdict underneath. Camera 1 looks for
        # missing parts (its verdict comes from the PLC); camera 2 checks the
        # stripes on the cable, judged by the app itself.
        live_height = round(self.LIVE_HEIGHT * self.image_scale)
        self.cam1_view = LiveCameraView(group(0, "CAMERA 1"), live_height, self.FONT, self.SKY,
                                        on_click=lambda: self.configure_camera(1))
        self.cam1_view.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.cam2_view = LiveCameraView(group(1, "CAMERA 2"), live_height, self.FONT, self.SKY,
                                        on_click=lambda: self.configure_camera(2))
        self.cam2_view.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.show_camera_idle(1)
        self.show_camera_idle(2)

        # Where the printed label gets scanned back in. It only opens while a
        # freshly printed label is waiting to be checked.
        self.label_scan_entry = tk.Entry(group(2, "PRINTED LABEL SCAN DATA"),
                                         bg=self.AQUA, fg='black',
                                         disabledbackground=self.AQUA,
                                         disabledforeground=self.PLACEHOLDER_INK,
                                         insertbackground='black',
                                         font=(self.FONT, 11, 'bold'), justify="center",
                                         relief="sunken", bd=1)
        # Its usual height, in the middle of the box the camera views make tall
        self.label_scan_entry.pack(fill="x", expand=True, padx=6, pady=(0, 6), ipady=16)
        self.label_scan_entry.insert(0, self.SCAN_PLACEHOLDER)
        self.label_scan_entry.configure(state='disabled')
        self.label_scan_entry.bind("<Return>", self.submit_label_scan)
        self.label_scan_entry.bind("<KeyRelease>", self.on_label_scan_key)

        spec_frame = tk.Frame(panel, bg='white', highlightthickness=1,
                              highlightbackground=self.EDGE)
        # Its height is fixed rather than taken from the rows, so stretching
        # the rows to fill it never makes the top row of the screen taller
        spec_frame.configure(height=self.SPEC_GRID_HEIGHT)
        spec_frame.pack_propagate(False)
        spec_frame.pack(fill="both", expand=True)

        # Column ids stay as the scoring code knows them; the headings are
        # what the operator reads.
        headings = {"Description": "DESCRIPTION", "Device": "DEVICE", "Unit": "UNIT",
                    "Min": "SPEC MIN", "Max": "SPEC MAX", "Actual": "ACTUAL",
                    "Result": "RESULT"}
        # Small starting widths that stretch to fill, so the grid fits
        # whatever width the screen leaves it
        widths = {"Description": 170, "Device": 60, "Unit": 55, "Min": 65,
                  "Max": 65, "Actual": 65, "Result": 65}
        self.spec_tree = ttk.Treeview(spec_frame, columns=tuple(headings), show="headings",
                                      height=8, style="Spec.Eol.Treeview")
        for column, heading in headings.items():
            self.spec_tree.heading(column, text=heading)
            self.spec_tree.column(column, width=widths[column], minwidth=40, stretch=True,
                                  anchor='w' if column == "Description" else 'center')
        self.spec_tree.pack(fill="both", expand=True)

        # Cell borders drawn over the grid, which ttk cannot draw itself
        self.spec_lines = []
        self.spec_fit_pending = False
        self.spec_tree.bind('<Configure>', lambda e: self.fit_spec_grid_soon())
        self.model_header.bind('<Configure>', lambda e: self.fit_spec_grid_soon())

        return panel

    # Height of the specification grid before its rows are stretched: a
    # heading and eight rows of the old size
    SPEC_GRID_HEIGHT = 290
    SPEC_ROW_HEIGHT = 30        # rows when no part is loaded
    SPEC_ROW_MIN = 20           # the least a row shrinks to, so a long spec still fits
    SPEC_LINE = '#808080'
    SPEC_RESULT_INK = {"PASS": '#0000FF', "NG": '#E00000'}

    def fit_spec_grid_soon(self):
        """Fit the grid once the layout settles; resizes come in bursts."""
        if not self.spec_fit_pending:
            self.spec_fit_pending = True
            self.root.after_idle(self.fit_spec_grid)

    def fit_spec_grid(self):
        """Match the heading to the part header, stretch the rows down the grid, draw borders.

        The heading row is as tall as MODEL NAME / PART NAME - PART NUMBER
        beside it, and the loaded rows share the height below it equally.
        """
        self.spec_fit_pending = False
        tree = self.spec_tree
        height = tree.winfo_height()
        header = self.model_header.winfo_height()
        if height <= 1 or header <= 1:
            return

        style = ttk.Style()
        heading_font = tkfont.Font(font=(self.FONT, 11, 'bold'))
        # The heading's own border takes a pixel above and below the text
        padding = max(2, (header - heading_font.metrics('linespace') - 2) // 2)
        style.configure("Spec.Eol.Treeview.Heading", padding=(4, padding))

        items = tree.get_children()
        row_height = self.SPEC_ROW_HEIGHT
        if items:
            row_height = max(self.SPEC_ROW_MIN, (height - header) // len(items))
        style.configure("Spec.Eol.Treeview", rowheight=row_height)
        tree.update_idletasks()

        for line in self.spec_lines:
            line.destroy()
        self.spec_lines = []
        if not items:
            return      # borders only once a part's specs are loaded

        def line(**place):
            mark = tk.Frame(tree, bg=self.SPEC_LINE)
            mark.place(**place)
            self.spec_lines.append(mark)

        # Where the first row starts is where the heading really ends
        top = header
        box = tree.bbox(items[0])
        if box:
            top = box[1]
        line(x=0, y=top - 1, relwidth=1, height=1)
        for index in range(1, len(items) + 1):
            y = top + index * row_height - 1
            if y < height - 1:
                line(x=0, y=y, relwidth=1, height=1)

        x = 0
        for column in tree['columns'][:-1]:
            x += tree.column(column, 'width')
            line(x=x - 1, y=0, width=1, relheight=1)

        # The rows stay white; only each result is coloured, written over
        # its own cell. The grid keeps the value underneath for scoring.
        columns = tree['columns']
        result_column = columns.index("Result")
        result_x = sum(tree.column(c, 'width') for c in columns[:result_column])
        result_width = tree.column("Result", 'width')
        for index, item in enumerate(items):
            result = tree.item(item, 'values')[result_column]
            ink = self.SPEC_RESULT_INK.get(result)
            if not ink:
                continue
            mark = tk.Label(tree, text=result, bg='white', fg=ink,
                            font=(self.FONT, 13, 'bold'))
            mark.place(x=result_x, y=top + index * row_height,
                       width=result_width - 1, height=row_height - 1)
            self.spec_lines.append(mark)

    def create_third_quadrant(self):
        """The load and length trend charts and the UPH chart, stacked on black."""
        panel = tk.Frame(self.workspace, bg='black', highlightthickness=1,
                         highlightbackground=self.EDGE)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        self.load_canvas = tk.Canvas(panel, bg='black', highlightthickness=0, height=120)
        self.load_canvas.grid(row=0, column=0, sticky="nsew")
        self.length_canvas = tk.Canvas(panel, bg='black', highlightthickness=0, height=120)
        self.length_canvas.grid(row=1, column=0, sticky="nsew")

        self.load_canvas.bind('<Configure>', lambda e: self.draw_load_graph())
        self.length_canvas.bind('<Configure>', lambda e: self.draw_length_graph())

        self.uph_canvas = tk.Canvas(panel, bg='black', highlightthickness=0, height=120)
        self.uph_canvas.grid(row=2, column=0, sticky="nsew")
        self.uph_canvas.bind('<Configure>', lambda e: self.draw_uph_graph())
        return panel

    def create_fourth_quadrant(self):
        """Today's results, the scan counters, and the operator's inputs."""
        panel = tk.Frame(self.workspace, bg=self.PANEL)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(0, weight=1)

        self.grid_frame = tk.Frame(panel, bg='white', highlightthickness=1,
                                   highlightbackground=self.EDGE)
        self.grid_frame.grid(row=0, column=0, sticky="nsew")

        self.current_columns = self.lot_columns(())
        self.create_lot_tree(self.grid_frame, self.current_columns)

        # The current lot and the scan counters, side by side under the grid
        strip = tk.Frame(panel, bg=self.PANEL)
        strip.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        strip.grid_columnconfigure(0, weight=1)
        strip.grid_columnconfigure(1, weight=0)
        self.create_lot_progress(strip).grid(row=0, column=0, sticky="nsew")
        self.create_scan_counters(strip).grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        inputs = tk.Frame(panel, bg=self.PANEL)
        inputs.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        inputs.grid_columnconfigure(0, weight=5)
        inputs.grid_columnconfigure(1, weight=2)
        inputs.grid_columnconfigure(2, weight=3)

        # Ends the run on this part so another can be loaded. Kept as an
        # attribute because next_model_command() disables it while the
        # closing NG cable check is outstanding.
        self.next_model_btn = tk.Button(inputs, text="CLICK HERE TO MOVE TO NEXT MODEL",
                                        bg=self.YELLOW, fg='black', activebackground='#E6E600',
                                        activeforeground='black', disabledforeground='#808080',
                                        font=(self.FONT, 13, 'bold'), relief='raised', bd=1,
                                        cursor='hand2', command=self.next_model_command)
        self.next_model_btn.grid(row=0, column=0, sticky="nsew")

        def code_box(column, fill):
            entry = tk.Entry(inputs, bg=fill, fg='black', disabledbackground=fill,
                             readonlybackground=fill, disabledforeground=self.PLACEHOLDER_INK,
                             insertbackground='black', font=(self.FONT, 13, 'bold'),
                             justify="center", relief="sunken", bd=1, width=12)
            entry.grid(row=0, column=column, sticky="nsew", padx=(4, 0), ipady=4)
            return entry

        # ALC Code Entry, disabled until an employee code is accepted
        self.alc_entry = code_box(1, self.AQUA)
        self.show_placeholder(self.alc_entry, self.ALC_PLACEHOLDER)
        self.alc_entry.config(state='disabled')

        self.emp_entry = code_box(2, self.YELLOW)
        self.show_placeholder(self.emp_entry, self.EMP_PLACEHOLDER)

        # Bind events
        self.emp_entry.bind("<FocusIn>", lambda e: self.on_emp_entry_focus(True))
        self.emp_entry.bind("<FocusOut>", lambda e: self.on_emp_entry_focus(False))
        self.emp_entry.bind("<Return>", self.validate_employee_code)

        self.alc_entry.bind("<FocusIn>", lambda e: self.on_alc_entry_focus(True))
        self.alc_entry.bind("<FocusOut>", lambda e: self.on_alc_entry_focus(False))
        self.alc_entry.bind("<Return>", self.process_alc_code)

        self.watch_placeholder(self.emp_entry)
        self.watch_placeholder(self.alc_entry)

        return panel

    def create_lot_progress(self, parent_frame):
        """Build the current lot box: OK cables, lot number, progress and trace codes.

        Until the operator sets a lot size, Progress shows only the size box;
        the count and bar join it once there is a size to count towards.
        """
        font = (self.FONT, 9, 'bold')
        box = tk.LabelFrame(parent_frame, text="CURRENT LOT PROGRESS", bg=self.GREEN,
                            fg='black', font=font, bd=1, relief='groove')

        def line():
            row = tk.Frame(box, bg=self.GREEN)
            row.pack(fill="x", padx=6, pady=(0, 2))
            return row

        def caption(row, text):
            tk.Label(row, text=text, bg=self.GREEN, fg='black', font=font).pack(side="left")

        def value(row, gap=8):
            label = tk.Label(row, text="", bg=self.GREEN, fg='black', font=font)
            label.pack(side="left", padx=(2, gap))
            return label

        first = line()
        caption(first, "Total OK Cables:")
        self.lot_total_label = value(first)
        caption(first, "LOT #:")
        self.lot_number_label = value(first)
        caption(first, "Progress:")

        progress = tk.Frame(first, bg=self.GREEN)
        progress.pack(side="left", padx=(2, 0))
        self.lot_count_label = tk.Label(progress, text="", bg=self.GREEN, fg='black', font=font)
        self.lot_count_label.grid(row=0, column=0)
        self.lot_slash_label = tk.Label(progress, text="/", bg=self.GREEN, fg='black', font=font)
        self.lot_slash_label.grid(row=0, column=1, padx=2)
        self.lot_size_entry = tk.Entry(progress, width=5, font=font, justify="center",
                                       relief="sunken", bd=1, fg='black',
                                       disabledforeground='black', insertbackground='black')
        self.lot_size_entry.grid(row=0, column=2)
        self.lot_size_entry.bind("<Return>", self.apply_lot_size)
        self.lot_size_entry.bind("<FocusOut>", self.apply_lot_size)
        self.lot_bar = tk.Frame(progress, bg='white', width=80, height=12,
                                relief="sunken", bd=1)
        self.lot_bar.grid(row=0, column=3, padx=(6, 0))
        self.lot_bar_fill = tk.Frame(self.lot_bar, bg=self.NAVY)

        second = line()
        caption(second, "Start TraceCode:")
        self.lot_start_label = value(second)
        caption(second, "Last OK:")
        self.lot_last_label = value(second)

        self.show_lot_progress()
        return box

    def create_scan_counters(self, parent_frame):
        """Build the OK / NG / invalid / total counters shown beside the lot box."""
        box = tk.LabelFrame(parent_frame, text="SCAN RESULT COUNTERS", bg=self.GREEN,
                            fg='black', font=(self.FONT, 9, 'bold'), bd=1, relief='groove')

        definitions = (("OK", "OK Count :"), ("INVALID", "*** Count :"),
                       ("NG", "NG Count :"), ("TOTAL", "Total Count :"))
        self.scan_count_labels = {}
        for position, (key, caption) in enumerate(definitions):
            row, column = divmod(position, 2)
            box.grid_columnconfigure(column * 2 + 1, weight=1)
            tk.Label(box, text=caption, bg=self.GREEN, fg='black',
                     font=(self.FONT, 9, 'bold')).grid(row=row, column=column * 2,
                                                       sticky="e", padx=(4, 2), pady=(0, 2))
            value = tk.Label(box, text="", bg='#E0FFFF', fg='black', width=5,
                             relief='sunken', bd=1, font=(self.FONT, 9, 'bold'))
            value.grid(row=row, column=column * 2 + 1, sticky="w", padx=(0, 4), pady=(0, 2))
            self.scan_count_labels[key] = value

        self.ok_count_label = self.scan_count_labels["OK"]
        self.ng_count_label = self.scan_count_labels["NG"]
        self.invalid_count_label = self.scan_count_labels["INVALID"]
        self.total_count_label = self.scan_count_labels["TOTAL"]
        return box

    def create_lot_tree(self, parent_frame, columns):
        """Create the lot tree with specified columns"""
        # Starting widths; every column stretches to fill the grid, so all
        # eight readings still fit on a narrow screen
        column_widths = {"Sl.No": 45, "LOT NUMBER": 100, "CAM1": 55, "CAM2": 55,
                         "RESULT": 60, "SCAN RESULT": 90}

        self.tree = ttk.Treeview(parent_frame,
                                 columns=columns,
                                 show="headings",
                                 height=10,
                                 style="Data.Eol.Treeview",
                                 yscrollcommand=lambda *_: self.draw_result_rows_soon())
        # Black lines under each result, drawn over the grid
        self.result_lines = []
        self.result_rows_pending = False
        self.tree.bind('<Configure>', lambda e: self.draw_result_rows_soon())

        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=column_widths.get(col, 42), minwidth=30,
                             stretch=True, anchor="center")

        self.tree.pack(side="left", fill="both", expand=True)

        # Configure scrolling using only the mouse wheel
        def on_mousewheel(event):
            self.tree.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def on_mousewheel_linux(event):
            if event.num == 4:
                self.tree.yview_scroll(-1, "units")
            elif event.num == 5:
                self.tree.yview_scroll(1, "units")

        def scroll(units):
            def handler(event):
                self.tree.yview_scroll(units, "units")
                return "break"
            return handler

        self.result_wheel = on_mousewheel   # the row lines pass the wheel on
        if sys.platform == "win32":
            self.tree.bind("<MouseWheel>", on_mousewheel)
        else:
            self.tree.bind("<Button-4>", on_mousewheel_linux)
            self.tree.bind("<Button-5>", on_mousewheel_linux)

        self.tree.bind("<Up>", scroll(-1))
        self.tree.bind("<Down>", scroll(1))
        self.tree.bind("<Prior>", scroll(-10))  # Page Up
        self.tree.bind("<Next>", scroll(10))    # Page Down

        # Bind double-click event to view details
        self.tree.bind("<Double-1>", self.on_tree_double_click)

    RESULT_LINE = '#000000'

    def draw_result_rows_soon(self):
        """Redraw the row lines once scrolling or filling settles."""
        if not self.result_rows_pending:
            self.result_rows_pending = True
            self.root.after_idle(self.draw_result_rows)

    def draw_result_rows(self):
        """Draw a black line under the heading and under each result on show.

        Rows are all one height and the grid scrolls a whole row at a time,
        so the lines sit on the visible rows' edges.
        """
        self.result_rows_pending = False
        tree = self.tree
        if not tree.winfo_exists():
            return
        for line in self.result_lines:
            line.destroy()
        self.result_lines = []

        items = tree.get_children()
        if not items:
            return      # lines only once there are results

        def line(y):
            mark = tk.Frame(tree, bg=self.RESULT_LINE)
            mark.place(x=0, y=y, relwidth=1, height=1)
            if sys.platform == "win32":
                mark.bind("<MouseWheel>", self.result_wheel)
            self.result_lines.append(mark)

        first = int(round(tree.yview()[0] * len(items)))
        drew_top = False
        for item in items[first:]:
            box = tree.bbox(item)
            if not box:
                break       # past the bottom of the grid
            x, y, width, height = box
            if not drew_top:
                line(y - 1)
                drew_top = True
            line(y + height - 1)

    def lot_columns(self, devices):
        """Results grid columns: L2-L4 and P3-P4 only when the part's spec lists them."""
        devices = set(devices or ())
        return (["Sl.No", "LOT NUMBER", "L1"]
                + [d for d in ("L2", "L3", "L4") if d in devices]
                + ["P1", "P2"]
                + [d for d in ("P3", "P4") if d in devices]
                + ["CAM1", "CAM2", "RESULT", "SCAN RESULT"])

    def update_tree_columns(self, devices=None):
        """Rebuild the results grid for the devices this part reports.

        display_data() fills it again once the part is loaded.
        """
        new_columns = self.lot_columns(devices)
        if new_columns != self.current_columns:
            self.tree.destroy()
            self.create_lot_tree(self.grid_frame, new_columns)
            self.current_columns = new_columns

    def on_tree_double_click(self, event):
        """Show the chosen result row on the status line."""
        try:
            item = self.tree.selection()[0]
            values = self.tree.item(item, "values")
            pairs = [f"{col}: {value}" for col, value in zip(self.current_columns, values)
                     if col != "Sl.No"]
            self.safe_update_message(", ".join(pairs), "blue")
        except IndexError:
            # No item selected
            pass
        except Exception as e:
            print(f"Error on tree double-click: {str(e)}")
            self.safe_update_message(f"Error displaying details: {str(e)}", "red")

    def populate_data_grid(self, rows):
        """Fill the results grid, banding every five rows so they are easy to scan."""
        try:
            self.tree.tag_configure('band', background='#FFFF99')
            self.tree.tag_configure('plain', background='#FFFFFF')

            for item in self.tree.get_children():
                self.tree.delete(item)

            fields = {"LOT NUMBER": 'TD_LOT_NUMBER', "RESULT": 'TD_OVERALL_STATUS',
                      "SCAN RESULT": 'TD_BARCODE_SCAN_RESULT'}
            for position, row in enumerate(rows):
                values = []
                for column in self.current_columns:
                    if column == "Sl.No":
                        values.append(position + 1)
                    else:
                        value = row.get(fields.get(column, column))
                        values.append('' if value is None else value)

                # Rows alternate in blocks of five
                tag = 'band' if (position // 5) % 2 == 0 else 'plain'
                self.tree.insert('', 'end', values=tuple(values), tags=(tag,))

            self.tree.selection_remove(self.tree.selection())
            self.draw_result_rows_soon()

        except Exception as e:
            print(f"Error populating data grid: {e}")

    # Trend chart colours, fixed per channel so L1 and P1 keep theirs whether
    # or not the part reports L2-L4
    SERIES_COLORS = ui.SERIES
    LOAD_COLORS = dict(zip(['L1', 'L2', 'L3', 'L4'], SERIES_COLORS))
    LENGTH_COLORS = dict(zip(['P1', 'P2', 'P3', 'P4'], SERIES_COLORS))
    CHART_GRID = ui.CHART_GRID

    def draw_graph(self, canvas, names, colors, default_labels, title):
        """Draw one chart: title, grid, y-axis scale, plotted series and legend."""
        canvas.delete("all")

        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 1:
            width = canvas.winfo_reqwidth()   # not mapped yet
        if height <= 1:
            height = canvas.winfo_reqheight()
        if width <= 1 or height <= 1:
            return

        left_margin = 44
        right_margin = 80   # room for the legend
        top_margin = 22     # room for the title
        bottom_margin = 20

        graph_width = width - (left_margin + right_margin)
        graph_height = height - (top_margin + bottom_margin)
        if graph_width <= 0 or graph_height <= 0:
            return

        label_font = (self.FONT, 8, 'bold')
        canvas.create_text(left_margin + graph_width / 2, top_margin / 2 + 1, text=title,
                           fill='white', font=(self.FONT, 9, 'bold'))

        names = self.visible_series(names)
        bounds = self.series_bounds(names)

        # Number of points across the x-axis, at least one grid span.
        point_count = max((len(self.chart_series.get(n, [])) for n in names), default=0)
        span = max(point_count - 1, 1)

        baseline = height - bottom_margin
        plot_right = width - right_margin
        dash = (6, 2, 2, 2, 2, 2)   # dash-dot-dot

        # Vertical grid on whole sample numbers, at most about eight of them,
        # so every label is a distinct test. An empty chart shows 1 to 8.
        first = self.dataPointX - point_count + 1 if point_count else 1
        x_span = span if point_count > 1 else 7
        tick = max(1, math.ceil(x_span / 8))
        for index in range(0, x_span + 1, tick):
            x = left_margin + (index * graph_width / x_span)
            canvas.create_line(x, top_margin, x, baseline, fill=self.CHART_GRID, dash=dash)
            canvas.create_text(x, baseline + 10, text=str(first + index),
                               fill='white', font=label_font)

        divisions = self.Y_DIVISIONS
        for i in range(divisions + 1):
            y = baseline - (i * graph_height / divisions)
            canvas.create_line(left_margin, y, plot_right, y, fill=self.CHART_GRID, dash=dash)
            if bounds:
                low, high = bounds
                value = low + (high - low) * i / divisions
                # Whole numbers lose the trailing ".00"; fine steps keep
                # enough places to stay distinct from their neighbours.
                step = (high - low) / divisions
                places = 0 if step >= 1 else (1 if step >= 0.1 else 2)
                label = f"{value:,.{places}f}"
            else:
                label = str(default_labels[i])
            canvas.create_text(left_margin - 6, y, text=label, anchor="e",
                               fill='white', font=label_font)

        # Plot each series as a smooth curve.
        if bounds and point_count > 1:
            low, high = bounds
            value_range = high - low

            for name in names:
                points = self.chart_series.get(name, [])
                if len(points) < 2:
                    continue

                coordinates = []
                for index, value in enumerate(points):
                    if value is None:
                        continue
                    x = left_margin + (index * graph_width / span)
                    y = baseline - ((value - low) / value_range) * graph_height
                    coordinates.extend((x, y))

                if len(coordinates) >= 4:
                    canvas.create_line(*coordinates, fill=colors[name], width=2,
                                       smooth=True, capstyle="round", joinstyle="round")

        # Legend in a white box, showing only the series this part reports.
        if names:
            legend_left = plot_right + 12
            legend_top = top_margin + 4
            canvas.create_rectangle(legend_left, legend_top, width - 8,
                                    legend_top + 8 + 14 * len(names),
                                    fill='white', outline=self.EDGE)
            y = legend_top + 11
            for name in names:
                canvas.create_line(legend_left + 6, y, legend_left + 22, y,
                                   fill=colors[name], width=2)
                canvas.create_text(legend_left + 28, y, text=name, anchor="w",
                                   fill='black', font=label_font)
                y += 14

    def draw_load_graph(self):
        self.draw_graph(self.load_canvas,
                        ['L1', 'L2', 'L3', 'L4'],
                        self.LOAD_COLORS,
                        [0, 20, 40, 60, 80, 100],
                        "LOAD - GRAPH")

    def draw_length_graph(self):
        self.draw_graph(self.length_canvas,
                        ['P1', 'P2', 'P3', 'P4'],
                        self.LENGTH_COLORS,
                        [-5, -3, -1, 1, 3, 5],
                        "LENGTH - GRAPH")

    # Bars on the UPH chart; the hour still running is drawn brighter
    UPH_BAR = '#00B000'
    UPH_BAR_NOW = '#7CFC00'

    def draw_uph_graph(self):
        """Draw today's OK cables per hour for the loaded part, one bar per hour.

        Margins match the trend charts above so the three plot areas line up.
        """
        canvas = self.uph_canvas
        canvas.delete("all")

        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 1:
            width = canvas.winfo_reqwidth()   # not mapped yet
        if height <= 1:
            height = canvas.winfo_reqheight()

        left_margin, right_margin, top_margin, bottom_margin = 44, 80, 22, 20
        graph_width = width - (left_margin + right_margin)
        graph_height = height - (top_margin + bottom_margin)
        if graph_width <= 0 or graph_height <= 0:
            return

        label_font = (self.FONT, 8, 'bold')
        canvas.create_text(left_margin + graph_width / 2, top_margin / 2 + 1,
                           text="UPH - GRAPH (OK CABLES PER HOUR)",
                           fill='white', font=(self.FONT, 9, 'bold'))

        # Whole-number ticks, at least 0 to 10 when there is little or nothing yet
        counts = self.uph_counts
        peak = max(counts.values(), default=0)
        step = max(math.ceil(self.nice_step(peak, self.Y_DIVISIONS)), 2) if peak else 2
        top = step * self.Y_DIVISIONS

        baseline = height - bottom_margin
        plot_right = width - right_margin
        dash = (6, 2, 2, 2, 2, 2)   # dash-dot-dot, as on the trend charts

        for i in range(self.Y_DIVISIONS + 1):
            y = baseline - (i * graph_height / self.Y_DIVISIONS)
            canvas.create_line(left_margin, y, plot_right, y, fill=self.CHART_GRID, dash=dash)
            canvas.create_text(left_margin - 6, y, text=f"{step * i:,}",
                               anchor="e", fill='white', font=label_font)

        # One slot per hour of the day; every other hour is labelled when
        # the slots get too narrow for all 24
        slot = graph_width / 24
        label_every = 1 if slot >= 22 else 2
        now = datetime.now().hour
        for hour in range(24):
            x = left_margin + hour * slot
            if hour % label_every == 0:
                canvas.create_text(x + slot / 2, baseline + 10, text=f"{hour:02d}",
                                   fill='white', font=label_font)
            count = counts.get(hour, 0)
            if not count:
                continue
            bar_top = baseline - (count / top) * graph_height
            canvas.create_rectangle(x + slot * 0.15, bar_top, x + slot * 0.85, baseline,
                                    fill=self.UPH_BAR_NOW if hour == now else self.UPH_BAR,
                                    outline="")
            if slot >= 18:
                canvas.create_text(x + slot / 2, bar_top - 6, text=str(count),
                                   fill='white', font=label_font)

        # This hour and the day so far, where the trend charts keep their legend
        box_left = plot_right + 12
        box_top = top_margin + 4
        canvas.create_rectangle(box_left, box_top, width - 8, box_top + 36,
                                fill='white', outline=self.EDGE)
        canvas.create_text(box_left + 6, box_top + 11, anchor="w", fill='black',
                           font=label_font, text=f"NOW  {counts.get(now, 0)}")
        canvas.create_text(box_left + 6, box_top + 25, anchor="w", fill='black',
                           font=label_font, text=f"DAY  {sum(counts.values())}")

    def load_uph(self):
        """Count today's OK cables for the loaded part by hour, then redraw the UPH chart."""
        counts = {}
        if self.partNumber:
            try:
                connection = self.get_database_connection()
                if connection:
                    cursor = connection.cursor()
                    cursor.execute("""
                        SELECT HOUR(TD_DATETIME), COUNT(*)
                        FROM TBL_TEST_DATA
                        WHERE TD_PART_NUMBER = %s AND TD_RECORD_DATE = CURDATE()
                          AND TD_OVERALL_STATUS = 'OK'
                        GROUP BY HOUR(TD_DATETIME)
                    """, (self.partNumber,))
                    counts = {int(hour): int(count) for hour, count in cursor.fetchall()
                              if hour is not None}
                    cursor.close()
                    connection.close()
            except Exception as e:
                print(f"Error loading UPH: {e}")
        self.uph_counts = counts
        try:
            self.draw_uph_graph()
        except Exception as e:
            print(f"Error drawing UPH graph: {e}")

    def show_placeholder(self, entry, text):
        """Put the grey watermark text in an empty code box."""
        state = entry.cget('state')
        entry.configure(state='normal')
        entry.delete(0, tk.END)
        entry.insert(0, text)
        entry.configure(fg=self.PLACEHOLDER_INK, state=state)
        # Remembered on the box, so the watermark is taken out however the
        # operator gets to it - not only when the box gains focus
        entry.placeholder = text

    def clear_placeholder(self, entry, text=None):
        """Take the watermark out so the operator types into an empty box."""
        if str(entry.cget('state')) != 'normal':
            return
        if getattr(entry, 'placeholder', None) and entry.get() == entry.placeholder:
            entry.delete(0, tk.END)
        entry.placeholder = None
        entry.configure(fg='black')

    def watch_placeholder(self, entry):
        """Clear a code box's watermark on the first key, click or paste.

        Focus alone isn't enough: after a test the ALC box gets its
        watermark back while it still has the focus, so clicking it again
        gains nothing, and what was typed went in grey after "ALC CODE".
        These run before the box's own handling, so the box is empty by the
        time the key is put in.
        """
        def on_key(event):
            if getattr(entry, 'placeholder', None) and (
                    (event.char and event.char.isprintable())
                    or event.keysym in ('BackSpace', 'Delete')):
                self.clear_placeholder(entry)

        def on_click_or_paste(event):
            if getattr(entry, 'placeholder', None):
                self.clear_placeholder(entry)

        entry.bind("<Key>", on_key, add="+")
        entry.bind("<Button-1>", on_click_or_paste, add="+")
        entry.bind("<<Paste>>", on_click_or_paste, add="+")

    def on_emp_entry_focus(self, is_focused):
        """Handle employee code entry focus"""
        if is_focused:
            self.clear_placeholder(self.emp_entry, self.EMP_PLACEHOLDER)
        elif not self.emp_entry.get():
            self.show_placeholder(self.emp_entry, self.EMP_PLACEHOLDER)

    def on_alc_entry_focus(self, is_focused):
        """Handle ALC entry focus"""
        if str(self.alc_entry.cget('state')) != 'normal':
            return
        if is_focused:
            self.clear_placeholder(self.alc_entry, self.ALC_PLACEHOLDER)
        elif not self.alc_entry.get():
            self.show_placeholder(self.alc_entry, self.ALC_PLACEHOLDER)

    def pick_image_scale(self):
        """How much of the 750x450 part image this screen has room for.

        A 1920x1080 screen shows it full size. A smaller one draws it, and the
        labels on it, smaller, so the charts and results grid below keep a
        usable height. Stored label positions stay in 750x450 terms.
        """
        room = self.root.winfo_screenheight() - self.CHROME_HEIGHT
        return max(0.5, min(1.0, room / self.IMAGE_HEIGHT))

    def show_image_placeholder(self):
        """Empty the part image area, ready for the next part."""
        if getattr(self, 'image_label', None) is not None:
            self.image_label.destroy()
        self.image_label = tk.Label(self.image_frame, text="No part loaded",
                                    bg='white', fg=self.PLACEHOLDER_INK,
                                    font=(self.FONT, 14, 'bold'))
        self.image_label.place(relx=0.5, rely=0.5, anchor='center')

    def update_camera_status(self, status_values):
        """Show camera 1's verdict from its OK and NG coils.

        ProcessStatus.txt lists them after the eight process steps. Whether
        camera 1 is on comes from its setup, not from the PLC. Lines without
        the coils have no camera, and cam1Result stays blank.
        """
        if len(self.process_addresses) < 10:
            return
        # The app checked camera 1 itself for this part; that verdict stands
        if self.cam1_by_app:
            return

        cam_ok = status_values.get(self.process_addresses[8], False)
        cam_ng = status_values.get(self.process_addresses[9], False)

        if not self.camera_check_on(1):
            text, fill = "OFF", self.SILVER
            self.cam1Result = "OFF"
        elif cam_ok and cam_ng:
            text, fill = "ERROR", '#FFA500'
        elif cam_ok:
            text, fill = "PASS", '#00FF00'
            self.cam1Result = "PASS"
        elif cam_ng:
            text, fill = "NG", '#FF0000'
            self.cam1Result = "NG"
        else:
            text, fill = "ON", self.SKY

        self.cam1_view.set_status(text, fill)

    def connect_to_devices(self):
        """Report the console ready once its window is built."""
        self.open_cameras()
        self.safe_update_message("System ready.", "green")

    @staticmethod
    def camera1_source():
        """Camera 1 is the camera Vision Settings sets up for inspection."""
        return load_vision_config().get("camera_source", "cam1")

    def open_cameras(self):
        """Keep the USB cameras streaming while the console is open.

        A camera opened cold needs about a second for its exposure to settle.
        Holding the streams open means each test's pictures, and the stripe
        check, use a settled frame straight away instead of adding that wait
        to every cycle. Pictures past their keeping time are cleared out here
        too.
        """
        if stripe_check is None:
            for view in (self.cam1_view, self.cam2_view):
                view.set_stream(None, "Camera support not installed")
            return
        self.start_camera_streams()
        threading.Thread(target=captures.remove_old, daemon=True).start()

    def unconfigured_cameras(self):
        """Names of the ticked cameras with no device chosen, e.g. ["Camera 2"]."""
        sources = ((1, self.camera1_source()), (2, stripe_check.CAMERA_SOURCE))
        return [f"Camera {number}" for number, source in sources
                if self.camera_check_on(number) and load_camera_config(source)[0] < 0]

    def cameras_ready_for_test(self):
        """True if a test may start; otherwise abort it and say which camera is missing.

        Each camera ticked must be set up, so a part is never tested on the
        assumption that a camera checked it. A camera unticked isn't used,
        and a test may start without it. Call this before the PLC is told to
        start.
        """
        if stripe_check is None:
            return True
        missing = self.unconfigured_cameras()
        if not missing:
            return True
        names = " and ".join(missing)
        verb = "are" if len(missing) > 1 else "is"
        self.log_operator_action("TEST_ABORTED", f"{names} not configured")
        self.safe_update_message(f"Test aborted - {names} {verb} not configured.", "red")
        messagebox.showerror(
            "Test Aborted",
            f"The test was not started: {names} {verb} not configured.\n\n"
            "Click the camera's picture on this screen to choose its device, then "
            "start the test again. To test without a camera, untick its CAM box.",
            parent=self.root)
        return False

    def start_camera_streams(self):
        """Open each camera that is set up and show it on its live view."""
        for source, view in ((self.camera1_source(), self.cam1_view),
                             (stripe_check.CAMERA_SOURCE, self.cam2_view)):
            index, width, height = load_camera_config(source)
            if index >= 0 and source not in self.camera_streams:
                self.camera_streams[source] = camera.acquire(index, width, height)
            view.set_stream(self.camera_streams.get(source), "Not set up\n(click to choose)")

    def stop_camera_streams(self, note=""):
        for view in (self.cam1_view, self.cam2_view):
            view.set_stream(None, note)
        for stream in self.camera_streams.values():
            stream.release()
        self.camera_streams = {}

    def configure_camera(self, number):
        """Choose the device for camera 1 or 2, from its live view.

        The dialog looks for cameras by opening each device in turn, and a
        device can only be opened once at a time, so the console lets go of
        its cameras while the dialog is open and takes them back after.
        """
        if stripe_check is None:
            return
        if self.camera_check_running:
            self.safe_update_message("Wait for the camera check to finish.", "red")
            return
        import vision_settings
        if number == 1:
            source, title = self.camera1_source(), "Camera 1 (missing parts)"
        else:
            source, title = stripe_check.CAMERA_SOURCE, "Camera 2 (stripes)"
        self.stop_camera_streams("Setting up…")
        was_on = self.camera_check_saved(number)
        self.camera_check_user = None
        try:
            vision_settings._open_camera_dialog(
                self.root, source, title, check_key=self.CAMERA_CHECK_KEYS[number],
                allow_check_change=lambda window: self.allow_camera_check_change(number,
                                                                                  window))
        finally:
            self.start_camera_streams()

        # It applies from the next test started
        on = self.camera_check_saved(number)
        self.camera_checks[number] = on
        self.show_camera_idle(number)
        if on != was_on:
            self.log_operator_action(f"CAM{number}_CHECK",
                                     f"{'On' if on else 'Off'} by {self.camera_check_user}")
            self.safe_update_message(
                f"Camera {number} check on - it checks each part." if on else
                f"Camera {number} check off - it will not check the parts.",
                "green" if on else "red")

    def grab_camera1(self):
        """A frame from camera 1, or None if it isn't a USB camera set up here."""
        index, width, height = load_camera_config(self.camera1_source())
        return camera.grab(index, width, height) if index >= 0 else None

    def show_camera_idle(self, number):
        """Show whether a camera is on or off, until a test gives it PASS or NG."""
        view = self.cam1_view if number == 1 else self.cam2_view
        if self.camera_check_on(number):
            view.set_status("ON", self.SKY)
        else:
            view.set_status("OFF", self.SILVER)

    def update_cam2_status(self, result):
        """Show camera 2's verdict: PASS, NG, ERROR, OFF or CHECKING."""
        fills = {"PASS": '#00FF00', "NG": '#FF0000', "ERROR": '#FFA500',
                 "OFF": self.SILVER, "CHECKING": self.SKY}
        self.cam2_view.set_status(result, fills.get(result, self.SKY))

    @staticmethod
    def verdict_of(result):
        """PASS, NG or ERROR for a vision or stripe check result."""
        return "PASS" if result.ok else "NG" if result.judgement == "NG" else "ERROR"

    def start_camera_checks(self):
        """Check the part on both cameras as its test starts.

        Camera 1 looks for missing parts with the check taught for the part
        in Vision Settings; camera 2 checks the stripes. Both run on a worker
        thread so the window keeps painting, and their verdicts show as soon
        as they are in, while the PLC carries on with the test. A part with
        nothing taught for a camera isn't checked on it: camera 1 then keeps
        the PLC's verdict, and camera 2 shows OFF. Each camera's picture is
        kept either way.
        """
        if self.camera_check_running:
            return
        # Every start is a new test, even after one abandoned before its
        # result, so nothing from an earlier check carries over
        self.camera_checks_done = False
        self.cam1_by_app = False
        if getattr(self, 'startingNGCableValidation', False) or \
                getattr(self, 'endingNGCableValidation', False):
            return      # a known-bad cable is checked by the PLC alone, and not saved
        cam1_on, cam2_on = self.camera_check_on(1), self.camera_check_on(2)
        if not (cam1_on or cam2_on):
            # Both cameras are off: neither judges the part, and camera 1
            # keeps the PLC's verdict
            self.cam2Result = "OFF"
            self.update_cam2_status("OFF")
            self.camera_checks_done = True
            return

        # A camera ticked off takes no picture and judges nothing: camera 1
        # then keeps the PLC's verdict, and camera 2 shows OFF
        part_number = self.partNumber
        vision = VisionController()
        check_cam1 = cam1_on and bool(part_number) and vision.has_model(part_number)
        check_cam2 = cam2_on and bool(part_number) and stripe_check.has_model(part_number)
        self.cam1Image = self.cam2Image = None
        self.camera_check_running = True
        self.cam1_by_app = check_cam1
        if check_cam1:
            self.cam1_view.set_status("CHECKING", self.SKY)
        if check_cam2:
            self.update_cam2_status("CHECKING")
        out = {}

        def work():
            try:
                if check_cam1:
                    result = vision.inspect(part_number)
                    out["cam1"] = result
                    verdict = self.verdict_of(result)
                    if result.frame is not None:
                        outline = []
                        if result.match_box:
                            x, y, w, h = result.match_box
                            outline = [np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
                                                np.float32)]
                        out["cam1_frame"] = captures.outlined(result.frame, outline, verdict)
                        out["cam1_image"] = captures.save(result.frame, "CAM1", part_number,
                                                          verdict, "", outline)
                elif cam1_on:
                    frame = self.grab_camera1()
                    if frame is not None:
                        out["cam1_frame"] = frame
                        out["cam1_image"] = captures.save(frame, "CAM1", part_number, "")
            except Exception as e:
                out["cam1_error"] = str(e)
            try:
                if check_cam2:
                    result = stripe_check.inspect(part_number)
                    out["cam2"] = result
                    verdict = self.verdict_of(result)
                    if result.frame is not None:
                        out["cam2_frame"] = captures.outlined(
                            result.frame, result.stripe_boxes, verdict)
                        out["cam2_image"] = captures.save(
                            result.frame, "CAM2", part_number, verdict,
                            result.seen, result.stripe_boxes)
                elif cam2_on:
                    frame = stripe_check.capture_frame()
                    if frame is not None:
                        out["cam2_frame"] = frame
                        out["cam2_image"] = captures.save(frame, "CAM2", part_number, "OFF")
            except Exception as e:
                out["cam2_error"] = str(e)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        def wait():
            if worker.is_alive():
                self.root.after(50, wait)
                return
            self.camera_check_running = False
            self.cam1Image = out.get("cam1_image")
            self.cam2Image = out.get("cam2_image")
            # Hold what was judged on screen until the next part
            self.cam1_view.hold(out.get("cam1_frame"))
            self.cam2_view.hold(out.get("cam2_frame"))
            problems = []

            if check_cam1:
                result = out.get("cam1")
                self.cam1Result = self.verdict_of(result) if result else "ERROR"
                fills = {"PASS": '#00FF00', "NG": '#FF0000', "ERROR": '#FFA500'}
                self.cam1_view.set_status(self.cam1Result, fills[self.cam1Result])
                if self.cam1Result != "PASS":
                    problems.append(f"Camera 1 {self.cam1Result}: "
                                    f"{result.error if result else out.get('cam1_error')}")
                print(f"[CAM1] {part_number}: {self.cam1Result} "
                      f"({result.error or 'part found' if result else out.get('cam1_error')})")

            if not check_cam2:
                self.cam2Result = "OFF"
            else:
                result = out.get("cam2")
                self.cam2Result = self.verdict_of(result) if result else "ERROR"
                if self.cam2Result != "PASS":
                    problems.append(f"Camera 2 {self.cam2Result}: "
                                    f"{result.error if result else out.get('cam2_error')}")
                print(f"[CAM2] {part_number}: {self.cam2Result} "
                      f"({(result.seen or result.error) if result else out.get('cam2_error')})")
            self.update_cam2_status(self.cam2Result)

            # Say so at once; the part is failed when the test is saved
            if problems:
                self.safe_update_message("  |  ".join(problems), "red")
            self.camera_checks_done = True
            then, self.after_camera_checks_do = self.after_camera_checks_do, None
            if then:
                then()

        wait()

    def after_camera_checks(self, then):
        """Call `then` once this test's camera checks are in.

        They normally finish long before the PLC does. If the result comes
        first, saving waits for them; if they never started, they run now.
        """
        if self.camera_checks_done:
            then()
            return
        self.after_camera_checks_do = then
        if not self.camera_check_running:
            self.start_camera_checks()
        # Checks that could not run at all count as done straight away
        if self.camera_checks_done and self.after_camera_checks_do:
            self.after_camera_checks_do = None
            then()

    def connect_to_plc(self):
        """Connect to PLC using configuration from .env file"""
        try:
            if self.plc_connected:
                print("PLC already connected")
                return True
                
            print("[*] Attempting to connect to PLC...")
            
            # Try TCP connection first if IP is configured
            if self.plc_tcp_ip:
                try:
                    self.plc_client = SerializedModbusClient(ModbusTcpClient(
                        host=self.plc_tcp_ip,
                        port=self.plc_tcp_port,
                        timeout=self.plc_timeout,
                        retries=self.plc_retries
                    ))
                    connection_result = self.plc_client.connect()
                    if connection_result:
                        self.plc_connected = True
                        self.plc_communication_errors = 0  # Reset error counter
                        self.plc_last_successful_read = time.time()  # Reset health timer
                        print(f"[OK] PLC connected via TCP - {self.plc_tcp_ip}:{self.plc_tcp_port}")
                        self.safe_update_message(f"PLC Connected via TCP: {self.plc_tcp_ip}", "green")
                        # Start automatic process status monitoring with reduced frequency
                        self.root.after(1000, self.start_plc_status_monitoring)
                        return True
                    else:
                        print("[ERROR] TCP connection failed, trying serial...")
                except Exception as e:
                    print(f"TCP connection error: {e}")
            
            # Try serial connection
            try:
                self.plc_client = SerializedModbusClient(ModbusSerialClient(
                    port=self.plc_com_port,
                    baudrate=self.plc_baud_rate,
                    bytesize=8,
                    parity='N',
                    stopbits=1,
                    timeout=self.plc_timeout,
                    retries=self.plc_retries
                ))
                connection_result = self.plc_client.connect()
                if connection_result:
                    self.plc_connected = True
                    self.plc_communication_errors = 0  # Reset error counter
                    self.plc_last_successful_read = time.time()  # Reset health timer
                    print(f"[OK] PLC connected via Serial - {self.plc_com_port}")
                    self.safe_update_message(f"PLC Connected via Serial: {self.plc_com_port}", "green")
                    # Start automatic process status monitoring with reduced frequency
                    self.root.after(1000, self.start_plc_status_monitoring)
                    return True
                else:
                    print("[ERROR] Serial connection failed")
                    self.safe_update_message("PLC connection failed - check configuration", "red")
                    return False
            except Exception as e:
                print(f"Serial connection error: {e}")
                self.safe_update_message(f"PLC connection error: {e}", "red")
                return False
                
        except Exception as e:
            print(f"Error connecting to PLC: {e}")
            self.safe_update_message(f"PLC connection error: {e}", "red")
            return False

    def disconnect_plc(self):
        """Disconnect from PLC"""
        try:
            if self.plc_client and self.plc_connected:
                self.status_monitoring_active = False  # Stop monitoring loops
                self.plc_thread_running = False  # Stop background thread
                self.plc_client.close()
                self.plc_connected = False
                print("PLC disconnected")
                self.safe_update_message("PLC Disconnected", "orange")
        except Exception as e:
            print(f"Error disconnecting PLC: {e}")



    def log_operator_action(self, action_type, description, employee_id=None):
        """Record an operator action in TBL_OPERATOR_LOG for the audit trail.

        If the database cannot take it, the entry goes to
        logs/operator_actions.log instead, so it is never silently lost.
        """
        now = datetime.now().replace(microsecond=0)
        emp_id = employee_id or getattr(self, 'current_employee_id', None) or ''
        part_number = getattr(self, 'current_part_number', None) or ''
        machine_id = getattr(self, 'machineID', '') or ''
        print(f"OPERATOR LOG: {now} | {emp_id} | {action_type} | {description} | Part: {part_number}")

        try:
            conn = db.connect(connection_timeout=5)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO TBL_OPERATOR_LOG (OL_DATETIME, OL_MACHINE_ID, OL_EMPLOYEE_CODE, "
                    "OL_ACTION, OL_DESCRIPTION, OL_PART_NUMBER) VALUES (%s, %s, %s, %s, %s, %s)",
                    (now, machine_id, emp_id, action_type, str(description)[:500], part_number))
                conn.commit()
                cursor.close()
            finally:
                conn.close()
        except Exception as e:
            print(f"Operator log not saved to database ({e}) - writing to file")
            try:
                log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
                os.makedirs(log_dir, exist_ok=True)
                with open(os.path.join(log_dir, 'operator_actions.log'), 'a', encoding='utf-8') as f:
                    f.write(f"{now}\t{machine_id}\t{emp_id}\t{action_type}\t{description}\t{part_number}\n")
            except OSError as file_error:
                print(f"Operator log not saved to file either: {file_error}")

    def execute_cycle_restart(self):
        """Execute the actual cycle restart"""
        try:
            print("🔄 EXECUTING CYCLE RESTART")
            released_by = self.current_employee_id

            # The part's unfinished lot ends with it; the lot box waits for
            # the next part
            self.close_partial_lot()
            self.lot_part = None
            self.open_lot = None
            self.show_lot_progress()

            # Clear current part number to force new selection
            if hasattr(self, 'current_part_number'):
                delattr(self, 'current_part_number')

            # The next part starts with a fresh login, as at startup: whoever
            # closed out this part is not necessarily who runs the next one.
            self.current_employee_id = None
            self.employee_validation_complete = False
            self.employee_validated = False
            self.emp_entry.configure(state='normal')
            self.show_placeholder(self.emp_entry, self.EMP_PLACEHOLDER)
            self.emp_entry.focus_set()

            # The ALC box waits, disabled, for that login
            self.show_placeholder(self.alc_entry, self.ALC_PLACEHOLDER)
            self.alc_entry.configure(state='disabled')

            # Nothing on show belongs to a loaded part any more
            self.model_header.configure(text="MODEL NAME / PART NAME - PART NUMBER")
            self.show_image_placeholder()

            # Reset spec tree
            if hasattr(self, 'spec_tree'):
                for item in self.spec_tree.get_children():
                    self.spec_tree.delete(item)
                self.fit_spec_grid_soon()
            
            # Reset test result saved flag
            self.test_result_saved = False

            # The sensor labels belong to the part that just left
            self.stop_all_label_blinking()
            for label in self.placed_labels.values():
                label.destroy()
            self.placed_labels = {}
            self.sensor_label_keys = []
            self.sensor_states = {}

            # next_model_command() disabled this while the closing NG cable
            # check ran; with the part released, the next one can use it.
            if getattr(self, 'next_model_btn', None):
                self.next_model_btn.configure(state='normal')

            # Show ready message
            self.safe_update_message("Part released - scan employee code to load the next part", "green")
            
            # Log the restart completion against the operator who released it
            self.log_operator_action("CYCLE_RESTART_COMPLETED", "Part released, operator logged out",
                                     released_by)
            
            print("[*] CYCLE RESTART COMPLETED - READY FOR NEW PART NUMBER")
            
        except Exception as e:
            print(f"Error executing cycle restart: {e}")
            self.safe_update_message(f"Error in cycle restart: {e}", "red")


    def visible_series(self, names):
        """Of the given series, the ones this part actually reports."""
        optional = {
            'L2': self.columnL2,
            'L3': self.columnL3,
            'L4': self.columnL4,
            'P3': self.columnP3,
            'P4': self.columnP4,
        }
        return [name for name in names if optional.get(name, True)]

    # The y-axis is divided into this many bands, so the scale has to land
    # on a step that divides cleanly by it.
    Y_DIVISIONS = 5

    @staticmethod
    def nice_step(rough, divisions):
        """A round step at least `rough`/divisions - 1, 2, 2.5 or 5 x 10^n.

        An axis read off raw data bounds is labelled 12.04, 26.41, 40.77;
        snapping the step first gives 10, 20, 30, which is what an operator
        can actually compare a reading against.
        """
        span = rough / divisions
        if span <= 0:
            return 1.0
        magnitude = 10 ** math.floor(math.log10(span))
        for multiple in (1, 2, 2.5, 5, 10):
            if span <= multiple * magnitude:
                return multiple * magnitude
        return 10 * magnitude

    def series_bounds(self, names):
        """Y-axis range covering the plotted points, snapped to round ticks."""
        values = [value for name in names for value in self.chart_series.get(name, [])]
        values = [v for v in values if v is not None]
        if not values:
            return None

        low, high = min(values), max(values)
        if low == high:
            # A flat line still needs a band to sit in.
            padding = abs(low) * 0.1 or 1.0
            low, high = low - padding, high + padding
        else:
            padding = (high - low) * 0.1
            low, high = low - padding, high + padding

        step = self.nice_step(high - low, self.Y_DIVISIONS)
        low = math.floor(low / step) * step
        high = low + step * self.Y_DIVISIONS
        return low, high


    def redraw_graphs(self):
        """Repaint both charts, ignoring canvases that are not on screen yet."""
        try:
            self.draw_load_graph()
            self.draw_length_graph()
        except Exception as e:
            print(f"Error drawing graphs: {e}")

    def start_label_drag(self, event):
        """Start dragging a label."""
        label = event.widget
        label._drag_start_x = event.x
        label._drag_start_y = event.y
        # Raise the label to the top of the stacking order
        label.lift()
        # Store original position
        label._original_position = (label.winfo_x(), label.winfo_y())

    def on_label_drag(self, event):
        """Handle label dragging."""
        label = event.widget
        # Calculate new position
        x = label.winfo_x() + event.x - label._drag_start_x
        y = label.winfo_y() + event.y - label._drag_start_y
        
        # Get image frame boundaries
        frame_width = self.image_frame.winfo_width()
        frame_height = self.image_frame.winfo_height()
        
        # Keep label within image frame boundaries
        x = max(0, min(x, frame_width - label.winfo_width()))
        y = max(0, min(y, frame_height - label.winfo_height()))
        
        # Move the label
        label.place(x=x, y=y)

    def stop_label_drag(self, event):
        """Handle end of label drag."""
        label = event.widget
        
        # Get label position relative to image frame
        x = label.winfo_x()
        y = label.winfo_y()
        
        # Update position in the positions dictionary
        label_key = getattr(label, 'label_key', label.cget('text'))
        self.label_positions[label_key] = (x, y)
        label.stored_position = (x / self.image_scale, y / self.image_scale)
        print(f"Label {label_key} dropped at x={x}, y={y}")
        
        # Save the updated positions to database
        self.save_label_positions()

    def test_result_command(self):
        """Score and record the test the PLC just finished.

        Every spec row is judged against its limits, then the result either
        settles an NG cable check or is saved as a production part.
        """
        try:
            print("=== TEST COMPLETION DETECTED ===")

            # With no part loaded there is nothing to score or save against.
            if not getattr(self, 'current_part_number', None):
                print("No part loaded - ignoring PLC result")
                self.rcvdTestRslt = False
                return

            self.score_spec_rows()
            kind = ("starting NG cable check" if self.startingNGCableValidation
                    else "closing NG cable check" if self.endingNGCableValidation
                    else "production part")
            self.trace(f"SCORED as {kind}: {self.passCounter} pass, {self.failCounter} fail")

            if self.startingNGCableValidation:
                # The operator ran the known-bad cable, so a failure is the
                # proof the rig still catches one. Nothing is saved.
                if self.failCounter > 0:
                    self.safe_update_message("NG Validation successful, continue to testing...", "green")
                    self.startingNGCableValidation = False
                    self.log_operator_action("NG_CHECK_START_PASSED", f"{self.failCounter} device(s) failed as expected")
                else:
                    self.safe_update_message("NG Validation NOT OK, please repeat NG Validation...", "red")
                    self.log_operator_action("NG_CHECK_START_FAILED", "Known-bad cable passed every device")
            elif self.endingNGCableValidation:
                if self.failCounter > 0:
                    self.safe_update_message("NG Validation successful.", "green")
                    self.log_operator_action("NG_CHECK_END_PASSED", f"{self.failCounter} device(s) failed as expected")
                    self.endingNGCableValidated = True
                    self.endingNGCableValidation = False
                    self.write_program_selection_to_plc(False)
                else:
                    self.safe_update_message("NG Validation NOT OK, please repeat NG Validation...", "red")
                    self.endingNGCableValidated = False
                    self.log_operator_action("NG_CHECK_END_FAILED", "Known-bad cable passed every device")
            else:
                # The cameras checked the part as the test started; it is
                # saved once their verdicts are in
                self.after_camera_checks(self.finish_test_cycle)
                return

        except Exception as e:
            print(f"Error in test_result_command: {e}")
            traceback.print_exc()
            self.safe_update_message(f"Error processing test results: {e}", "red")

        self.root.after(1200, self.prepare_next_cycle)

    def finish_test_cycle(self):
        """Save the scored part, then get ready for the next one."""
        try:
            self.complete_test_cycle()
        finally:
            self.root.after(1200, self.prepare_next_cycle)

    def score_spec_rows(self):
        """Fill Actual and Result on every spec row and count passes and fails."""
        self.failCounter = 0
        self.passCounter = 0

        for item in self.spec_tree.get_children():
            device = str(self.spec_tree.item(item, "values")[1])
            value = self.get_actual_value_for_device(device)
            shown = f"{value:.1f}" if device.startswith("L") else f"{value:.2f}"
            self.update_specification_result(device, shown)

            if self.spec_tree.item(item, "values")[-1] == "PASS":
                self.passCounter += 1
            else:
                self.failCounter += 1

    def prepare_next_cycle(self):
        """Clear the finished test and hand the machine the next part."""
        try:
            self.trace("NEXT CYCLE - readings cleared, Machine On for the next part")
            self.reset_test_parameters()
            self.reset_dgv_spec_data()
            self.write_machine_on_to_plc()

            if self.endingNGCableValidated:
                # The closing check passed, so this part is done with.
                self.endingNGCableValidated = False
                self.execute_cycle_restart()
        except Exception as e:
            print(f"Error preparing next cycle: {e}")
            traceback.print_exc()

    def next_model_command(self):
        """Finish with this part so a different one can be loaded.

        Moving to another model closes out the current part, and the machine
        is not allowed to simply stop: the operator has to pull a known-bad
        cable through first, which proves the rig still reports a failure.
        So this clears the readings and restarts the check in
        ending-validation mode, where test_result_command() waits for that
        failure before the part is released.
        """
        # With no part loaded there is nothing to close out, and the check
        # would never release the button again.
        if not getattr(self, 'current_part_number', None):
            messagebox.showinfo("Next Model", "No part is loaded.")
            return

        if not messagebox.askyesno("Confirm Move",
                                   "Do you really want to move on to a different part?"):
            return

        try:
            self.log_operator_action("NEXT_MODEL", "Closing NG cable check started")
            self.trace("NEXT MODEL pressed - run the NG cable to release the part")
            self.breakLoop = True

            # One closing validation at a time - execute_cycle_restart()
            # turns the button back on once the check releases the part.
            if getattr(self, 'next_model_btn', None):
                self.next_model_btn.configure(state='disabled')

            self.endingNGCableValidation = True

            self.reset_dgv_spec_data()
            self.reset_test_parameters()

            # After the resets, not before: reset_test_parameters() blanks the
            # status line, so setting the prompt first would wipe the one
            # instruction the operator needs here.
            self.safe_update_message("Please Validate NG Cable...", "blue")

            self.start_check_async()
        except Exception as e:
            print(f"Error starting next model: {e}")
            messagebox.showerror("Next Model", f"Could not start the next model: {e}")

    def setup_barcode_listener(self):
        """Alternative approach using tkinter bindings"""
        self.root.bind('<Key>', self.on_key_press)
        self.root.bind('<Return>', self.on_enter_press)
        self.barcode_data = ""  # Initialize empty barcode data

    def on_key_press(self, event):
        """Handle key press events for barcode scanning"""
        if event.char and event.char.isprintable():
            # Only accumulate data if we're not directly focused on an entry widget
            focused_widget = self.root.focus_get()
            if not isinstance(focused_widget, tk.Entry):
                self.barcode_data += event.char
            # If focused on an entry, let the default behavior happen

    def on_enter_press(self, event):
        """Handle Enter key for barcode scanning"""
        # Process barcode if not in an entry widget
        focused_widget = self.root.focus_get()
        if not isinstance(focused_widget, tk.Entry) and self.barcode_data:
            self.process_barcode_data()
        # If Enter is pressed in an entry, let the default behavior happen
        # Barcode data is cleared in process_barcode_data when used

    def process_barcode_data(self):
        """Process the captured barcode data."""
        barcode = ''.join(c for c in self.barcode_data if c.isprintable()).strip()
        if not barcode:
            return

        # A printed label waiting to be checked takes the scan, even if the
        # scan box lost focus - otherwise it would be read as an ALC code.
        if self.awaiting_label_scan:
            self.label_scan_entry.delete(0, tk.END)
            self.label_scan_entry.insert(0, barcode)
            self.barcode_data = ""
            self.submit_label_scan()
            return

        # Check if we're waiting for employee code
        if not self.emp_entry.get() or self.emp_entry.get() == self.EMP_PLACEHOLDER:
            self.clear_placeholder(self.emp_entry)
            self.emp_entry.delete(0, tk.END)
            self.emp_entry.insert(0, barcode)
            self.validate_employee_code()
            # Reset barcode data after processing
            self.barcode_data = ""
            return
            
        # If ALC code is enabled and a barcode is scanned, process it
        elif self.alc_entry.cget('state') == 'normal':
            self.clear_placeholder(self.alc_entry)
            self.alc_entry.delete(0, tk.END)
            self.alc_entry.insert(0, barcode)
            self.process_alc_code()
            # Reset barcode data after processing
            self.barcode_data = ""
            return
            
        # Ready for testing when both codes are entered
        elif (hasattr(self, 'current_part_number') and self.current_part_number and 
              self.emp_entry.get() and self.emp_entry.get() != self.EMP_PLACEHOLDER and
              self.alc_entry.get() and self.alc_entry.get() != self.ALC_PLACEHOLDER):
            
            self.safe_update_message("Ready for testing - waiting for test completion", "green")
            print("System ready for testing")
            
            # Reset barcode data after processing
            self.barcode_data = ""
            return
            
        # Reset barcode data if not used
        self.barcode_data = ""

    def get_database_connection(self):
        """Get MySQL database connection"""
        try:
            connection = mysql.connector.connect(**self.db_config)
            return connection
        except mysql.connector.Error as e:
            print(f"Database connection error: {e}")
            messagebox.showerror("Database Error", f"Failed to connect to database: {e}")
            return None

    def test_database_connection(self):
        """Test database connectivity and create tables if needed"""
        try:
            connection = self.get_database_connection()
            if connection and connection.is_connected():
                cursor = connection.cursor()
                
                # Create necessary tables
                self.create_database_tables(cursor)
                
                connection.commit()
                cursor.close()
                connection.close()
                return True
                
        except mysql.connector.Error as e:
            print(f"Database test failed: {e}")
            messagebox.showerror("Database Error", f"Database test failed: {e}")
            return False

    def create_database_tables(self, cursor):
        """Create the database tables used by the EOL test flow."""
        db.init_database(raise_on_error=True)
        print("Database tables created/verified successfully")

    def load_configuration_data(self):
        """Load the PLC addresses and employee codes from the data files"""
        try:
            # Load ProcessStatus
            process_status_path = data_files.path(data_files.PROCESS_STATUS)
            if os.path.exists(process_status_path):
                with open(process_status_path, 'r') as file:
                    self.process_status_array = [line.strip() for line in file.readlines()]
            else:
                print(f"Warning: ProcessStatus.txt not found at {process_status_path}")
                self.process_status_array = []
            
            # Load ProgramSelectionInPLC
            program_selection_path = data_files.path(data_files.PROGRAM_SELECTION_IN_PLC)
            if os.path.exists(program_selection_path):
                with open(program_selection_path, 'r') as file:
                    self.program_selection_array = [line.strip() for line in file.readlines()]
            else:
                print(f"Warning: ProgramSelectionInPLC.txt not found at {program_selection_path}")
                self.program_selection_array = []
            
            # Load InputSensors
            input_sensors_path = data_files.path(data_files.INPUT_SENSORS)
            if os.path.exists(input_sensors_path):
                with open(input_sensors_path, 'r') as file:
                    self.input_sensors_array = [line.strip() for line in file.readlines()]
            else:
                print(f"Warning: InputSensors.txt not found at {input_sensors_path}")
                self.input_sensors_array = []
            
            # Load EmployeeCodes
            employee_codes_path = data_files.path(data_files.EMPLOYEE_CODES)
            if os.path.exists(employee_codes_path):
                with open(employee_codes_path, 'r') as file:
                    self.employee_codes = [line.strip() for line in file.readlines()]
            else:
                print(f"Warning: EmployeeCodes.txt not found at {employee_codes_path}")
                self.employee_codes = []
            
            # Load PLC On Register file
            plc_register_path = data_files.path(data_files.PLC_ON_REGISTER)
            if os.path.exists(plc_register_path):
                with open(plc_register_path, 'r') as file:
                    # Process the file content as needed
                    print(f"Loaded register file from: {plc_register_path}")
            else:
                print(f"Warning: PLC_on_register.txt not found at {plc_register_path}")
            
            # Load barcode print filenames
            barcode_print_path = data_files.path(data_files.BARCODE_PRINT_FILE_NAMES)
            if os.path.exists(barcode_print_path):
                with open(barcode_print_path, 'r') as file:
                    # Process the file content as needed
                    print(f"Loaded barcode print filenames from: {barcode_print_path}")
            else:
                print(f"Warning: BarcodePrintFileNames.txt not found at {barcode_print_path}")
            
            # Load Machine On PLC Coil Address
            machine_on_path = data_files.path(data_files.MACHINE_ON_PLC_COIL_ADDRESS)
            if os.path.exists(machine_on_path):
                with open(machine_on_path, 'r') as file:
                    self.machineOnPLCCoilAddress = file.read().strip()
                    print(f"Loaded Machine On PLC Coil Address: {self.machineOnPLCCoilAddress}")
            else:
                print(f"⚠️ WARNING: MachineOnPLCCoilAddress.txt not found at {machine_on_path}")
                print("   This file is REQUIRED for PLC to start test!")
                self.machineOnPLCCoilAddress = ""
            
            # Load the START button's coil, which has an address of its own
            # unless the file names another
            start_path = data_files.path(data_files.START_BUTTON_PLC_COIL_ADDRESS)
            self.startButtonPLCCoilAddress = self.START_BUTTON_ADDRESS
            if os.path.exists(start_path):
                with open(start_path, 'r') as file:
                    self.startButtonPLCCoilAddress = file.read().strip()
            print(f"START button PLC Coil Address: {self.startButtonPLCCoilAddress}")

            # Load Alert On PLC Coil Address
            alert_on_path = data_files.path(data_files.ALERT_ON_PLC_COIL_ADDRESS)
            if os.path.exists(alert_on_path):
                with open(alert_on_path, 'r') as file:
                    self.alertOnPLCCoilAddress = file.read().strip()
                    print(f"Loaded Alert On PLC Coil Address: {self.alertOnPLCCoilAddress}")
            else:
                print(f"Warning: AlertOnPLCCoilAddress.txt not found at {alert_on_path}")
                self.alertOnPLCCoilAddress = ""
            
            # Check if any array is empty
            if not all([self.process_status_array, self.program_selection_array, 
                       self.input_sensors_array, self.employee_codes]):
                print("Warning: One or more configuration files are empty or not found.")
                # Only try to update message_label if it exists and the window is valid
                self.safe_update_message("Warning: Some configuration files not found. Check the project folder.", "orange")
            
        except FileNotFoundError as e:
            print(f"File Not Found: {str(e)}")
            self.safe_update_message(f"File Not Found: {str(e)}", "red")
        except Exception as e:
            print(f"Error loading configuration data: {str(e)}")
            self.safe_update_message(f"Error loading configuration data: {str(e)}", "red")

    def load_authorized_employee_codes(self):
        """Load authorized employee codes from EmployeeCodes.txt"""
        try:
            employee_codes_path = data_files.path(data_files.EMPLOYEE_CODES)
            
            if os.path.exists(employee_codes_path):
                with open(employee_codes_path, 'r') as file:
                    # Read all lines and strip whitespace
                    self.authorized_employee_codes = [line.strip() for line in file.readlines() if line.strip()]
                print(f"Loaded {len(self.authorized_employee_codes)} authorized employee codes")
                return True
            else:
                print(f"Warning: EmployeeCodes.txt not found at {employee_codes_path}")
                # Create default file with sample employee code
                with open(employee_codes_path, 'w') as file:
                    file.write("S041\n")
                self.authorized_employee_codes = ["S041"]
                print("Created default EmployeeCodes.txt with sample code S041")
                return True
                
        except Exception as e:
            print(f"Error loading employee codes: {e}")
            self.safe_update_message(f"Error loading employee codes: {e}", "red")
            return False

    def disable_controls_pending_employee_validation(self):
        """Disable controls until employee validation is complete"""
        try:
            # Disable part number entry
            if hasattr(self, 'alc_entry'):
                self.alc_entry.config(state='disabled')
            
            print("Controls disabled pending employee validation")
            
        except Exception as e:
            print(f"Error disabling controls: {e}")

    def initialize_employee_validation(self):
        """Initialize employee validation on startup"""
        try:
            # Disable all controls initially
            self.disable_controls_pending_employee_validation()
            
            # Show welcome message
            self.safe_update_message("Welcome! Employee validation required to proceed.", "blue")
            
            # Load employee codes
            self.load_authorized_employee_codes()
            
            print("Employee validation system initialized")
            
        except Exception as e:
            print(f"Error initializing employee validation: {e}")
            self.safe_update_message(f"Error initializing employee validation: {e}", "red")
            
    def read_process_status_values(self):
        """
        Read process status values from PLC - Enhanced Error Handling
        
        Reads the process status coils:
        - Read coils individually
        - Return dictionary of address:value pairs
        - Return an empty dict when the PLC cannot be read. Never invent
          coil states: a made-up TESTRESULT would be saved as a real test.
        - Better connection health monitoring
        """
        try:
            # Check if PLC is connected with better error handling
            if not hasattr(self, 'plc_client') or not self.plc_client:
                print("⚠️ PLC client not initialized - no status read")
                return {}
            
            # Check socket status with detailed error reporting
            try:
                if not self.plc_client.is_socket_open():
                    print("⚠️ PLC socket closed - attempting reconnection")
                    if not self.connect_to_plc():
                        print("⚠️ PLC reconnection failed - no status read")
                        return {}
            except Exception as e:
                print(f"⚠️ PLC socket check failed: {e} - no status read")
                return {}
            
            # Read from PLC
            status_values = {}
            
            # Load process addresses if not loaded
            if not hasattr(self, 'process_addresses'):
                self.load_process_addresses()
            
            if not hasattr(self, 'process_addresses') or not self.process_addresses:
                print("⚠️ No process addresses loaded - no status read")
                return {}
            
            station_id = int(config.get('PLC_STATION_ID', '1'))
            
            # All the coils come back in one request covering the lowest to
            # the highest address. One request per coil took long enough
            # that a result coil the PLC holds only briefly could be on and
            # off again between two reads.
            try:
                # Word in decimal, bit in hex: see plc_address.py
                bits = {address: bit_address(address)
                        for address in self.process_addresses
                        if address.strip() and address[0] in 'MX'}
                if not bits:
                    return {}
                first = min(bits.values())
                count = max(bits.values()) - first + 1
                if count > 2000:
                    print("⚠️ Process coils span more than one Modbus read - no status read")
                    return {}

                result = self.plc_client.read_coils(first, count=count, device_id=station_id)
                if result.isError():
                    self.plc_communication_errors += 1
                    print(f"⚠️ PLC read error for process coils: {result}")
                    return {}

                for address, bit in bits.items():
                    status_values[address] = bool(result.bits[bit - first])
                self.plc_last_successful_read = time.time()
                self.plc_communication_errors = 0

            except Exception as e:
                print(f"Error reading PLC coils: {e}")
                self.plc_communication_errors += 1
                return {}

            return status_values
                
        except Exception as e:
            print(f"Error in read_process_status_values: {e}")
            self.plc_communication_errors += 1
            return {}

    def load_hold_register_addresses(self):
        """Load hold register addresses from InputRegisters.txt file"""
        try:
            hold_registers_file = data_files.path(data_files.INPUT_REGISTERS)
            
            if os.path.exists(hold_registers_file):
                with open(hold_registers_file, 'r') as file:
                    content = file.read().strip()
                    if content:
                        self.hold_register_addresses = [addr.strip() for addr in content.split(',')]
                        print(f"Loaded hold register addresses: {self.hold_register_addresses}")
                    else:
                        # Default addresses if file is empty
                        self.hold_register_addresses = ["D001", "D002", "D003", "D004", "D005", "D006", "D007", "D008"]
                        print("Using default hold register addresses")
            else:
                # Default addresses if file doesn't exist
                self.hold_register_addresses = ["D001", "D002", "D003", "D004", "D005", "D006", "D007", "D008"]
                print("InputRegisters.txt not found, using default addresses")
                
        except Exception as e:
            print(f"Error loading hold register addresses: {e}")
            self.hold_register_addresses = ["D001", "D002", "D003", "D004", "D005", "D006", "D007", "D008"]
    
    def load_process_addresses(self):
        """Load process addresses from ProcessStatus.txt file"""
        try:
            process_status_file = data_files.path(data_files.PROCESS_STATUS)
            
            if os.path.exists(process_status_file):
                with open(process_status_file, 'r') as file:
                    content = file.read().strip()
                    if content:
                        self.process_addresses = [addr.strip() for addr in content.split(',')]
                        print(f"Loaded process addresses: {self.process_addresses}")
                    else:
                        # Default addresses if file is empty
                        self.process_addresses = ["M100", "M101", "M102", "M103", "M104", "M105", "M106", "M107"]
                        print("Using default process addresses")
            else:
                # Default addresses if file doesn't exist
                self.process_addresses = ["M100", "M101", "M102", "M103", "M104", "M105", "M106", "M107"]
                print("ProcessStatus.txt not found, using default addresses")
                
        except Exception as e:
            print(f"Error loading process addresses: {e}")
            # Fallback to default addresses
            self.process_addresses = ["M100", "M101", "M102", "M103", "M104", "M105", "M106", "M107"]
            
    def safe_update_message(self, message, color="black"):
        """Safely update message label with proper error handling"""
        try:
            if hasattr(self, 'root') and self.root.winfo_exists():
                if hasattr(self, 'message_label') and self.message_label and self.message_label.winfo_exists():
                    self.message_label.show(message, color)
                else:
                    print(f"Cannot display message '{message}' - message_label not ready")
            else:
                print(f"Cannot display message '{message}' - root window not ready")
        except Exception as e:
            print(f"Error updating message: {e}")
            print(f"Original message was: {message}")

    def cleanup(self):
        """Enhanced cleanup method with forced resource release for PLC connection"""
        try:
            # Stop all monitoring first
            self.keepWriting = False
            self.breakLoop = True
            
            # Stop PLC background thread
            if hasattr(self, 'plc_thread_running'):
                self.plc_thread_running = False
                print("PLC background thread stopped")
            
            # Stop PLC status monitoring
            if hasattr(self, 'status_monitoring_active'):
                self.status_monitoring_active = False
                print("Status monitoring stopped")
            
            # Stop all blinking labels
            self.stop_all_label_blinking()

            self.stop_camera_streams()
            
            # Clean up loadcell connections
            if hasattr(self, 'loadcell1_client') and self.loadcell1_client:
                try:
                    if self.loadcell1_client.is_open:
                        self.loadcell1_client.close()
                    print("Loadcell 1 connection closed successfully")
                except Exception as e:
                    print(f"Error closing Loadcell 1 connection: {e}")
                finally:
                    self.loadcell1_client = None
            
            if hasattr(self, 'loadcell2_client') and self.loadcell2_client:
                try:
                    if self.loadcell2_client.is_open:
                        self.loadcell2_client.close()
                    print("Loadcell 2 connection closed successfully")
                except Exception as e:
                    print(f"Error closing Loadcell 2 connection: {e}")
                finally:
                    self.loadcell2_client = None
            
            # Release the part's program so the PLC is not left running it
            # after the console closes.
            if self.programSelectionPLCAddress and self.plc_client:
                self.write_program_selection_to_plc(False)

            # PLC client cleanup
            self.disconnect_plc()
            
            # Give time for ports to be released
            # Non-blocking delay - operation will complete asynchronously
            
            # Additional force close to ensure COM ports are released
            self.force_close_com_ports()
            
            print("All connections cleaned up successfully")
                
        except Exception as e:
            print(f"Error during cleanup: {str(e)}")
    
    def force_close_com_ports(self):
        """Force close COM ports that might be in use with enhanced error handling"""
        try:
            # Get all available ports first
            available_ports = [p.device for p in list_ports.comports()]
            print(f"Available COM ports: {available_ports}")
            
            # PLC port handling removed
                
            # Also check if any loadcell ports need to be closed
            loadcell1_port = config.get('LOADCELL_01_COM_PORT')
            if loadcell1_port and loadcell1_port.strip():
                if loadcell1_port not in available_ports:
                    print(f"Warning: Configured Loadcell 1 port {loadcell1_port} is not available on this system")
                else:
                    self._force_close_port(loadcell1_port)
                    
            loadcell2_port = config.get('LOADCELL_02_COM_PORT')
            if loadcell2_port and loadcell2_port.strip():
                if loadcell2_port not in available_ports:
                    print(f"Warning: Configured Loadcell 2 port {loadcell2_port} is not available on this system")
                else:
                    self._force_close_port(loadcell2_port)
                    
            # Force garbage collection to ensure any lingering port references are cleaned up
            import gc
            gc.collect()
            
        except Exception as e:
            print(f"Error force-closing COM ports: {e}")
            
    def _force_close_port(self, port):
        """Helper method to force close a single COM port with multiple attempts"""
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                # Try with different timeouts and settings
                if attempt == 0:
                    # First attempt - standard settings
                    test_serial = serial.Serial(port, timeout=0.5)
                elif attempt == 1:
                    # Second attempt - shorter timeout
                    test_serial = serial.Serial(port, timeout=0.1, baudrate=9600)
                else:
                    # Third attempt - different settings
                    test_serial = serial.Serial(
                        port, 
                        baudrate=9600, 
                        bytesize=8,
                        parity='N',
                        stopbits=1,
                        timeout=0.1
                    )
                    
                # Successfully opened, now close it
                test_serial.close()
                print(f"Successfully force-closed {port} on attempt {attempt+1}")
                return True
                
            except serial.SerialException as e:
                if "Access is denied" in str(e) or "Port is in use" in str(e):
                    print(f"Attempt {attempt+1}: Port {port} is in use, trying different approach...")
                    # Wait briefly before next attempt
                    # Non-blocking delay - operation will complete asynchronously
                else:
                    print(f"Could not force-close {port} on attempt {attempt+1}: {e}")
                    
            except Exception as e:
                print(f"Error on attempt {attempt+1} to force-close {port}: {e}")
                
        print(f"Failed to force-close {port} after {max_attempts} attempts")
        return False

    def get_absolute_image_path(self, db_image_path):
        """Convert database image path to absolute path if needed"""
        if not db_image_path:
            return None
        
        # If path is already absolute, return it
        if os.path.isabs(db_image_path):
            return db_image_path
        
        # Otherwise, assume it's relative to the application directory
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, db_image_path)

    def load_image_with_path(self, image_path):
        """Load and fit image to match the exact dimensions of model_settings.py"""
        try:
            if not os.path.exists(image_path):
                raise FileNotFoundError(f"Image file not found: {image_path}")
            
            # Use the same exact fixed dimensions as model_settings.py for perfect label alignment
            target_width = round(self.IMAGE_WIDTH * self.image_scale)
            target_height = round(self.IMAGE_HEIGHT * self.image_scale)
            
            print(f"Loading image with fixed dimensions: {target_width}x{target_height}")
            
            # Load and resize image to exactly match the model settings dimensions
            original_image = Image.open(image_path)
            resized_image = original_image.resize((target_width, target_height), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(resized_image)
            
            # Remove old image label if it exists
            if hasattr(self, 'image_label'):
                self.image_label.destroy()
            
            # Create new image label with exact same dimensions
            self.image_label = tk.Label(self.image_frame, image=photo, bg='white')
            self.image_label.image = photo  # Keep a reference
            self.image_label.place(x=0, y=0, relwidth=1, relheight=1)
            
            # Store image dimensions for label positioning
            self.image_dimensions = {
                'width': target_width,
                'height': target_height,
                'x_offset': 0,
                'y_offset': 0
            }
            
            # Force update of the display before returning
            self.root.update_idletasks()
            
            self.current_image_path = image_path
            print(f"Successfully loaded image: {image_path}")
            return True
            
        except Exception as e:
            print(f"Error loading image: {str(e)}")
            messagebox.showerror("Error", f"Error loading image: {str(e)}")
            return False

    def place_labels_from_positions(self, coordinates_data):
        """Place labels L1-L15 according to database coordinates with improved visibility and blinking effect."""
        try:
            # Clear any existing placed labels and stop any blinking
            self.stop_all_label_blinking()
            for label in getattr(self, 'placed_labels', {}).values():
                label.destroy()
            self.placed_labels = {}
            self.label_positions = {}  # Reset positions dictionary
            
            # Get image frame dimensions for validation
            frame_width = self.image_frame.winfo_width()
            frame_height = self.image_frame.winfo_height()
            
            print(f"Image frame dimensions: {frame_width}x{frame_height}")
            print(f"Placing labels with coordinates: {coordinates_data}")
            
            # Make sure image is fully loaded before placing labels
            self.root.update_idletasks()
            
            # Create all labels L1-L15 if they exist in coordinates_data
            for i in range(1, self.LABEL_COUNT + 1):
                label_num = str(i)
                if label_num in coordinates_data:
                    try:
                        coord_data = coordinates_data[label_num]
                        # Get coordinates from database
                        # Stored against 750x450; drawn at the screen's scale
                        x = float(coord_data.get('x', 0)) * self.image_scale
                        y = float(coord_data.get('y', 0)) * self.image_scale
                        
                        print(f"Processing label L{label_num} at coordinates ({x}, {y})")
                        
                        # Create label with enhanced visibility. label_text is
                        # the canonical key. Model Settings sets the text shown
                        # while the sensor is off and while it is on; parts saved
                        # before that use their caption for both. Text saved
                        # as the bare key shows the label's name.
                        label_text = f'L{label_num}'
                        caption = data_files.shown_label_text(
                            self.label_names, label_text, coord_data.get('text'))
                        off_text = data_files.shown_label_text(
                            self.label_names, label_text, coord_data.get('off_text') or caption)
                        on_text = data_files.shown_label_text(
                            self.label_names, label_text, coord_data.get('on_text') or caption)
                        new_label = tk.Label(self.image_frame,
                                           text=off_text,
                                           bg="yellow",  # Initial background color
                                           fg="black",
                                           font=("Arial", max(8, round(12 * self.image_scale)), "bold"),
                                           width=max(4, len(on_text), len(off_text)),
                                           relief="raised",
                                           borderwidth=2)
                        
                        # Place label at exact coordinates
                        new_label.place(x=x, y=y)
                        
                        # Ensure label is on top of all other widgets
                        new_label.lift()
                        
                        # Enable dragging for the label
                        new_label.bind("<Button-1>", self.start_label_drag)
                        new_label.bind("<B1-Motion>", self.on_label_drag)
                        new_label.bind("<ButtonRelease-1>", self.stop_label_drag)
                        new_label.configure(cursor="hand2")
                        
                        # Store the label and its position
                        new_label.label_key = label_text
                        new_label.caption = caption
                        new_label.on_text = on_text
                        new_label.off_text = off_text
                        # Its position in Model Settings' 750x450 terms, kept
                        # exact so re-saving never rounds it off the screen scale
                        new_label.stored_position = (float(coord_data.get('x', 0)),
                                                     float(coord_data.get('y', 0)))
                        self.placed_labels[label_text] = new_label
                        self.label_positions[label_text] = (x, y)
                        
                        # Store original color for blinking
                        new_label.original_bg = "yellow"
                        
                        print(f"Successfully placed {label_text} at coordinates x={x}, y={y}")
                        
                    except Exception as e:
                        print(f"Error placing label L{label_num}: {str(e)}")
                        continue
            
            # Start blinking effect for all placed labels
            self.start_label_blinking()

            # Hand the worker thread the sensors to read for these labels
            self.sensor_states = {}
            self.sensor_label_keys = list(self.placed_labels)
                
        except Exception as e:
            print(f"Error placing labels: {e}")
            messagebox.showerror("Error", f"Failed to place labels: {str(e)}")

    def start_label_blinking(self):
        """Start blinking effect for all placed labels."""
        if not hasattr(self, 'blinking_jobs'):
            self.blinking_jobs = {}
        
        # Start blinking for each label
        for label_text, label in self.placed_labels.items():
            if label_text not in self.blinking_jobs:
                self.blink_label(label, label_text)
    
    def blink_label(self, label, label_text):
        """Create blinking effect for a label, alternating between red and original color."""
        if not hasattr(label, 'blink_state'):
            label.blink_state = False
        
        # Toggle blink state
        label.blink_state = not label.blink_state
        
        # Set background color based on blink state
        if label.blink_state:
            label.configure(bg="red")
        else:
            label.configure(bg=label.original_bg)
        
        # Store the job ID to be able to cancel it later
        self.blinking_jobs[label_text] = self.root.after(500, lambda: self.blink_label(label, label_text))
    
    def stop_all_label_blinking(self):
        """Stop blinking effect for all labels."""
        if hasattr(self, 'blinking_jobs'):
            for job_id in self.blinking_jobs.values():
                self.root.after_cancel(job_id)
            self.blinking_jobs = {}
    
    def stop_label_blinking(self, label_text):
        """Stop blinking effect for a specific label."""
        if hasattr(self, 'blinking_jobs') and label_text in self.blinking_jobs:
            self.root.after_cancel(self.blinking_jobs[label_text])
            del self.blinking_jobs[label_text]

    def save_label_positions(self):
        """Save current label positions to database."""
        if not hasattr(self, 'current_part_number') or not self.placed_labels:
            return

        positions = {}
        for label_text, label in self.placed_labels.items():
            if label_text.startswith('L'):
                label_num = label_text[1:]  # Extract number from "L1", "L2", etc.
                # The label's text follows its sensor, so save the texts set
                # in Model Settings rather than whatever it shows right now.
                stored_x, stored_y = label.stored_position
                positions[label_num] = {
                    'x': round(stored_x),
                    'y': round(stored_y),
                    'text': label.caption,
                    'on_text': label.on_text,
                    'off_text': label.off_text,
                }

        try:
            conn = db.connect()
            cursor = conn.cursor()

            # Update the database with new positions
            positions_json = json.dumps(positions)
            update_query = """
            UPDATE TBL_MODEL_MASTER 
            SET MM_LABEL_COORDINATES = %s 
            WHERE MM_PART_NUMBER = %s
            """
            cursor.execute(update_query, (positions_json, self.current_part_number))
            conn.commit()

            print(f"Saved label positions to database for part number {self.current_part_number}")
            print(f"Positions: {positions_json}")

            cursor.close()
            conn.close()

        except mysql.connector.Error as err:
            print(f"Database Error: {err}")
            messagebox.showerror("Database Error", f"Failed to save label positions: {err}")
        except Exception as e:
            print(f"Error: {e}")
            messagebox.showerror("Error", f"An unexpected error occurred: {e}")

        # Start your monitoring threads/processes here

    def validate_employee_code(self, event=None):
        """Validate employee code against EmployeeCodes.txt"""
        emp_code = self.emp_entry.get().strip()
        if emp_code == self.EMP_PLACEHOLDER or not emp_code:
            messagebox.showwarning("Warning", "Please enter an employee code")
            return
        
        try:
            # Check for employee codes file
            employee_codes_path = data_files.path(data_files.EMPLOYEE_CODES)
            
            # Check if file exists
            if not os.path.exists(employee_codes_path):
                messagebox.showerror("Error", "Employee Codes text file is either missing or empty!!")
                return
            
            # Read employee codes
            with open(employee_codes_path, 'r') as file:
                content = file.read().strip()
                if content:
                    self.employeeCodesArray = content.split(',')
                    # Clean up employee codes (remove whitespace)
                    self.employeeCodesArray = [code.strip() for code in self.employeeCodesArray]
                else:
                    messagebox.showerror("Error", "Employee Codes text file is either missing or empty!!")
                    return
            
            # Validate employee code
            if emp_code in self.employeeCodesArray:
                # Set employee validation flags
                self.current_employee_id = emp_code
                self.employee_validation_complete = True
                self.employee_validated = True
                
                # Make employee entry read-only
                self.emp_entry.configure(state='readonly', fg='black')
                
                # Enable ALC entry and set focus
                self.alc_entry.configure(state='normal')
                self.alc_entry.focus_set()
                
                self.safe_update_message("Employee code validated - Enter ALC code", "green")
                
                print(f"Employee {emp_code} validated successfully")
                
                # Initialize database connection test
                self.test_database_connection()
                self.log_operator_action("LOGIN", "Employee code accepted", emp_code)
                
            else:
                messagebox.showerror(
                    "Unauthorized Employee", 
                    f"Employee code: {emp_code} is NOT AUTHORIZED to operate this machine, please consult SUPERVISOR."
                )
                self.log_operator_action("LOGIN_REFUSED", "Employee code not authorised", emp_code)
                self.emp_entry.delete(0, tk.END)
                self.emp_entry.focus_set()
                return
                
        except Exception as e:
            print(f"Error validating employee code: {e}")
            messagebox.showerror("Error", f"Error reading employee codes: {e}")
            # Reset validation flags on error
            self.current_employee_id = None
            self.employee_validation_complete = False
            return

    def process_alc_code_cs_style(self, alc_code):
        """Load the part for a scanned ALC code and start testing it."""
        try:
            # Make ALC entry read-only
            self.alc_entry.configure(state='readonly')
            
            part_exists = False
            self.barcodePrintFileName = ""
            self.prnFileContent = ""
            
            # Database connection using configured settings
            connection = self.get_database_connection()
            if not connection:
                messagebox.showerror("Database Error", "Failed to connect to database")
                return
                
            cursor = connection.cursor(dictionary=True)

            # Query TBL_MODEL_MASTER for part information
            model_query = """
            SELECT 
                MM_PART_NUMBER,
                MM_MODEL_NAME,
                MM_VENDOR_CODE,
                MM_EO_NUMBER,
                MM_SPECIAL_DATA,
                MM_INITIAL_ID,
                MM_SUPPLIER_SECTION,
                MM_IMAGE_PATH,
                MM_BARCODE_LABEL_CODE,
                MM_PLC_ADDRESS,
                MM_LABEL_COORDINATES
            FROM TBL_MODEL_MASTER 
            WHERE MM_ALC_CODE = %s AND MM_STATUS = %s
            """
            cursor.execute(model_query, (alc_code, True))
            model_result = cursor.fetchone()
            
            if model_result:
                # Store part information
                self.current_alc_code = alc_code
                self.partNumber = model_result['MM_PART_NUMBER']
                self.modelName = model_result['MM_MODEL_NAME'] 
                self.vendorCode = model_result['MM_VENDOR_CODE'] or ""
                self.eoNumber = model_result['MM_EO_NUMBER'] or ""
                self.specialData = model_result['MM_SPECIAL_DATA'] or ""
                self.initialID = model_result['MM_INITIAL_ID'] or ""
                self.supplierSection = model_result['MM_SUPPLIER_SECTION'] or ""
                
                # Update part name label
                if hasattr(self, 'model_header'):
                    self.model_header.configure(text=f"{self.modelName} - {self.partNumber}")
                
                # Load part image, then the sensor labels that sit on it
                image_path = self.get_absolute_image_path(model_result['MM_IMAGE_PATH'])
                if image_path and os.path.exists(image_path) and self.load_image_with_path(image_path):
                    coordinates = model_result['MM_LABEL_COORDINATES']
                    if coordinates:
                        try:
                            self.place_labels_from_positions(json.loads(coordinates))
                        except json.JSONDecodeError:
                            messagebox.showwarning("Warning", "Invalid label coordinate data")
                
                # Handle barcode print file
                self.barcodePrintFileName = model_result['MM_BARCODE_LABEL_CODE'] or ""
                if self.barcodePrintFileName and self.barcodePrintFileName != "NO_BARCODE_PRINT_FILE":
                    self.barcodePrintFileNamePath = data_files.path(self.barcodePrintFileName)
                    if os.path.exists(self.barcodePrintFileNamePath):
                        with open(self.barcodePrintFileNamePath, 'r') as f:
                            self.prnFileContent = f.read()
                
                # The writes below start the test, so the cameras are checked
                # first: with a camera's box ticked and that camera not set
                # up, the part stays on show but the test is aborted
                if not self.cameras_ready_for_test():
                    # Not loaded for testing: START asks for the ALC code
                    # again rather than run the previous part's program
                    self.current_part_number = None
                    cursor.close()
                    connection.close()
                    self.alc_entry.configure(state='normal')
                    self.alc_entry.delete(0, tk.END)
                    self.show_placeholder(self.alc_entry, self.ALC_PLACEHOLDER)
                    return

                # ===================================================================
                # NOTE: Write to PLC during ALC code processing
                # These writes TRIGGER the PLC to start the test automatically
                # ===================================================================
                
                # Get PLC program selection address from database
                self.programSelectionPLCAddress = model_result['MM_PLC_ADDRESS']
                
                print(f"\n{'='*60}")
                print("🎯 PLC INITIALIZATION")
                print(f"{'='*60}")
                
                # Write Program Selection to PLC
                if self.programSelectionPLCAddress:
                    print(f"📝 Program Selection Address: {self.programSelectionPLCAddress}")
                    success = self.write_program_selection_to_plc()
                    if success:
                        print(f"✅ PLC Program Selected - Test will start automatically")
                    else:
                        print(f"❌ Failed to write Program Selection - PLC may not start")
                else:
                    print(f"⚠️ No Program Selection address in database - skipping")
                
                # Write Machine On signal to PLC
                if self.machineOnPLCCoilAddress:
                    print(f"📝 Machine On Address: {self.machineOnPLCCoilAddress}")
                    success = self.write_machine_on_to_plc()
                    if success:
                        print(f"✅ Machine On written - PLC should start running now")
                    else:
                        print(f"❌ Failed to write Machine On - PLC will not start")
                else:
                    print(f"❌ ERROR: MachineOnPLCCoilAddress not configured!")
                    print(f"   Check MachineOnPLCCoilAddress.txt exists in the project folder")
                    messagebox.showwarning("Configuration Error", 
                        "Machine On PLC Coil Address is not configured!\n" +
                        "Test cannot start without this address.")
                
                print(f"{'='*60}\n")
                
                part_exists = True
            
            if part_exists:
                # Load model specifications
                self.load_model_specifications(cursor)
                
                # Load label details for dynamic UI
                self.load_model_label_details(cursor)
                
                # Initialize data display and graphs
                self.display_data()
                self.load_graph()
                
                # Get lot number for this part
                self.get_lot_number()
                
                # ===================================================================
                # NOTE: Auto-start test after ALC code processing
                # Immediately calls start_check_async
                # Test begins automatically without button click
                # ===================================================================
                
                print("\n✅ Part loaded successfully!")
                self.trace(f"PART {self.partNumber} loaded from ALC {alc_code} - "
                           f"program {self.programSelectionPLCAddress}, "
                           f"{len(self.deviceToRead)} spec row(s): {' '.join(self.deviceToRead)}")
                print("🚀 Starting test automatically - no button click required")
                
                # Set current part number for testing
                self.current_part_number = self.partNumber
                self.log_operator_action("PART_LOADED", f"ALC {alc_code}: {self.modelName}")
                
                # Starting NG cable check switched off for now: the first
                # cable after loading is tested as a production part
                # self.startingNGCableValidation = True
                
                # Update status message
                self.safe_update_message(
                    f"Part loaded - next LOT: {self.next_lot_number()} - Waiting for PLC pull1 state...", 
                    "orange"
                )
                
                # ===================================================================
                # WAIT FOR PULL1 STATE BEFORE STARTING TEST
                # System must wait for PLC to return to pull1 position/state
                # Test will not start until pull1 state is confirmed
                # ===================================================================
                print("⏳ Waiting for PLC to return to pull1 state before starting test...")
                self.wait_for_pull1_and_start_test()
                
                # The ALC code stays on show, read-only, while its part is
                # loaded; releasing the part clears it for the next login
                
            else:
                messagebox.showwarning("Part Not Found", "Scanned Part Does NOT Exist...")
                self.alc_entry.delete(0, tk.END)
                self.alc_entry.configure(state='normal')
                self.alc_entry.focus_set()
            
            cursor.close()
            connection.close()
            
        except Exception as e:
            print(f"Error processing ALC code: {e}")
            messagebox.showerror("Error", f"Error processing ALC code: {e}")
            self.alc_entry.delete(0, tk.END)
            self.alc_entry.configure(state='normal')
            self.alc_entry.focus_set()

    def load_model_specifications(self, cursor):
        """Load model specifications from database"""
        try:
            spec_query = """
            SELECT 
                MS_DESCRIPTION,
                MS_DEVICE,
                MS_NORMAL_MIN,
                MS_UNIT,
                MS_NORMAL_MAX
            FROM TBL_MODEL_SPECIFICATION 
            WHERE MS_PART_NUMBER = %s
            ORDER BY MS_DEVICE
            """
            cursor.execute(spec_query, (self.partNumber,))
            specifications = cursor.fetchall()
            
            # Clear existing specification tree
            if hasattr(self, 'spec_tree'):
                self.spec_tree.delete(*self.spec_tree.get_children())
            
            # Reset device visibility flags
            self.columnL2 = self.columnL3 = self.columnL4 = False
            self.columnP3 = self.columnP4 = False
            self.deviceToRead = []
            
            # Process each specification
            for spec in specifications:
                device = spec['MS_DEVICE'].upper()
                self.deviceToRead.append(device)
                
                # Set column visibility flags
                if device == "L2":
                    self.columnL2 = True
                elif device == "L3":
                    self.columnL3 = True
                elif device == "L4":
                    self.columnL4 = True
                elif device == "P3":
                    self.columnP3 = True
                elif device == "P4":
                    self.columnP4 = True
                
                # Add to specification tree
                if hasattr(self, 'spec_tree'):
                    values = (
                        spec['MS_DESCRIPTION'],
                        device,
                        spec['MS_UNIT'],
                        f"{float(spec['MS_NORMAL_MIN']):.2f}" if spec['MS_NORMAL_MIN'] is not None else "N/A",
                        f"{float(spec['MS_NORMAL_MAX']):.2f}" if spec['MS_NORMAL_MAX'] is not None else "N/A",
                        "",  # Actual value (empty initially)
                        ""   # Result (empty initially)
                    )
                    self.spec_tree.insert('', 'end', values=values)
            
            # Update tree columns based on devices
            if hasattr(self, 'spec_tree'):
                self.update_tree_columns(set(self.deviceToRead))
                self.fit_spec_grid_soon()
                
        except Exception as e:
            print(f"Error loading model specifications: {e}")

    def load_model_label_details(self, cursor):
        """Load model label details for dynamic UI"""
        try:
            label_query = """
            SELECT 
                MLD_LABEL_ID,
                MLD_ON_STATUS,
                MLD_OFF_STATUS,
                MLD_X,
                MLD_Y,
                MLD_FONT
            FROM TBL_MODEL_LABEL_DETAILS 
            WHERE MLD_PART_NUMBER = %s
            """
            cursor.execute(label_query, (self.partNumber,))
            label_details = cursor.fetchall()
            
            self.mldDataTable = label_details
            self.inputSensorsToReadList = []
            
            # Process each label detail
            for label in label_details:
                if label['MLD_ON_STATUS'] and label['MLD_ON_STATUS'].strip():
                    self.inputSensorsToReadList.append(label['MLD_LABEL_ID'])
                    
                    # Create or update label widget (simplified for now)
                    # In full implementation, this would create dynamic labels on the UI
                    print(f"Label {label['MLD_LABEL_ID']}: {label['MLD_OFF_STATUS']} -> {label['MLD_ON_STATUS']}")
                    
        except Exception as e:
            print(f"Error loading model label details: {e}")

    def trace(self, message):
        """One timestamped line in the terminal on what the test is doing."""
        print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} [TEST] {message}")

    def write_named_coil(self, name, address, value):
        """Write one M coil, tracing what was written and how the PLC answered."""
        state = 'ON' if value else 'off'
        try:
            # M address to its Modbus coil number (see plc_address.py)
            coil = bit_address(address)
            result = self.plc_client.write_coil(coil, value, device_id=self.plc_station_id)
        except Exception as e:
            self.trace(f"WRITE {name} {address} = {state} -> FAILED: {e}")
            self.last_plc_error = str(e)
            return False
        if result.isError():
            self.trace(f"WRITE {name} {address} (coil {coil}) = {state} -> PLC REFUSED: {result}")
            self.last_plc_error = f"coil {coil}: {result}"
            return False
        self.trace(f"WRITE {name} {address} (coil {coil}) = {state} -> accepted")
        return True

    def write_program_selection_to_plc(self, value=True):
        """
        Write program selection to PLC. False releases the program once the
        closing NG cable check has passed.
        Returns: bool - Success status
        """
        if not self.plc_client:
            self.trace("WRITE PROGRAM SELECT skipped - the PLC is not connected")
            return False
        if not self.programSelectionPLCAddress or not self.programSelectionPLCAddress.startswith('M'):
            self.trace(f"WRITE PROGRAM SELECT skipped - the part's PLC address "
                       f"'{self.programSelectionPLCAddress}' is not an M coil")
            return False
        return self.write_named_coil('PROGRAM SELECT', self.programSelectionPLCAddress, value)

    # How long Machine On is held off before it is switched on
    MACHINE_ON_OFF_TIME = 0.5

    def write_machine_on_to_plc(self):
        """
        Switch Machine On off, then on, so the PLC always sees it come on.

        Machine On left on by a test that never finished would otherwise
        stay on, and writing on again would not start the next test.
        Returns: bool - Success status
        """
        if not self.plc_client:
            self.trace("WRITE MACHINE ON skipped - the PLC is not connected")
            return False
        if not self.machineOnPLCCoilAddress or not self.machineOnPLCCoilAddress.startswith('M'):
            self.trace(f"WRITE MACHINE ON skipped - MachineOnPLCCoilAddress.txt holds "
                       f"'{self.machineOnPLCCoilAddress}', not an M coil")
            return False
        if not self.write_named_coil('MACHINE ON', self.machineOnPLCCoilAddress, False):
            return False
        time.sleep(self.MACHINE_ON_OFF_TIME)
        return self.write_named_coil('MACHINE ON', self.machineOnPLCCoilAddress, True)

    # The PLC's START push button, used when StartButtonPLCCoilAddress.txt
    # is not there, and how long the console holds it pressed
    START_BUTTON_ADDRESS = "M000A"
    START_BUTTON_PRESS_TIME = 0.5

    def press_start_button_on_plc(self):
        """Press and release the PLC's START coil, as the push button would.

        Released even when the press failed part way, so START is never
        left held on.
        Returns: bool - Success status
        """
        address = self.startButtonPLCCoilAddress
        if not self.plc_client:
            self.trace("WRITE START skipped - the PLC is not connected")
            return False
        if not address or not address.startswith('M'):
            self.trace(f"WRITE START skipped - StartButtonPLCCoilAddress.txt holds "
                       f"'{address}', not an M coil")
            return False
        pressed = self.write_named_coil('START', address, True)
        if pressed:
            time.sleep(self.START_BUTTON_PRESS_TIME)
        released = self.write_named_coil('START', address, False)
        return pressed and released

    def display_data(self):
        """Show today's passing results for the current part and refresh the counters."""
        if not self.partNumber:
            self.reset_scan_result_counters()
            return

        try:
            connection = self.get_database_connection()
            if not connection:
                return

            cursor = connection.cursor(dictionary=True)
            cursor.execute("""
                SELECT *
                FROM TBL_TEST_DATA
                WHERE TD_RECORD_DATE = CURDATE()
                  AND TD_PART_NUMBER = %s
                  AND TD_OVERALL_STATUS = 'OK'
                ORDER BY ID DESC
            """, (self.partNumber,))
            rows = cursor.fetchall()
            cursor.close()
            connection.close()

            self.update_scan_result_counters(rows)
            self.populate_data_grid(rows)
            self.load_lot_progress()
            print(f"Displaying {len(rows)} test records for part: {self.partNumber}")

        except Exception as e:
            print(f"Error displaying test data: {e}")

    def update_scan_result_counters(self, rows):
        """Count OK, NG and invalid barcode scans across the displayed rows."""
        try:
            results = [str(row.get('TD_BARCODE_SCAN_RESULT') or '') for row in rows]
            self.ok_count_label.configure(text=str(results.count('OK')))
            self.ng_count_label.configure(text=str(results.count('NG')))
            self.invalid_count_label.configure(text=str(results.count('***')))
            self.total_count_label.configure(text=str(len(rows)))
        except Exception as e:
            print(f"Error updating scan result counters: {e}")

    def reset_scan_result_counters(self):
        """Blank the scan counters, ready for the next part."""
        try:
            for label in (self.ok_count_label, self.ng_count_label,
                          self.invalid_count_label, self.total_count_label):
                label.config(text="")
        except Exception as e:
            print(f"Error resetting scan result counters: {e}")

    def load_lot_progress(self):
        """Read the part's open lot and running totals into the lot box.

        A newly loaded part starts from the size of its last lot; after that
        the size the operator sets stays until the part is released.
        """
        if not self.partNumber:
            return

        try:
            connection = self.get_database_connection()
            if not connection:
                return

            cursor = connection.cursor(dictionary=True)
            cursor.execute("""
                SELECT COALESCE(SUM(LT_OK_COUNT), 0) AS TOTAL_OK
                FROM TBL_PART_LOT
                WHERE LT_PART_NUMBER = %s
            """, (self.partNumber,))
            total_ok = int(cursor.fetchone()['TOTAL_OK'])
            cursor.execute("""
                SELECT *
                FROM TBL_PART_LOT
                WHERE LT_PART_NUMBER = %s
                ORDER BY LT_LOT_NUMBER DESC
                LIMIT 1
            """, (self.partNumber,))
            latest = cursor.fetchone()
            cursor.close()
            connection.close()

            if self.lot_part != self.partNumber:
                self.lot_part = self.partNumber
                self.lot_size = latest['LT_LOT_SIZE'] if latest else None

            self.lot_total_ok = total_ok
            if latest and latest['LT_STATUS'] == 'OPEN':
                self.open_lot = latest
                self.lot_size = latest['LT_LOT_SIZE'] or self.lot_size
                self.lot_number = latest['LT_LOT_NUMBER']
            else:
                # The next OK cable starts the next lot
                self.open_lot = None
                self.lot_number = (latest['LT_LOT_NUMBER'] if latest else 0) + 1

            self.show_lot_progress()

        except Exception as e:
            print(f"Error loading lot progress: {e}")

    def show_lot_progress(self):
        """Fill the lot box from the lot state; blank it when no part is loaded."""
        loaded = self.lot_part is not None
        lot = self.open_lot
        count = lot['LT_OK_COUNT'] if lot else 0

        self.lot_total_label.configure(text=str(self.lot_total_ok) if loaded else "")
        self.lot_number_label.configure(text=str(self.lot_number) if loaded else "")
        self.lot_start_label.configure(text=(lot['LT_START_TRACE_CODE'] or "") if lot else "")
        self.lot_last_label.configure(text=(lot['LT_LAST_TRACE_CODE'] or "") if lot else "")

        # Yellow asks for a size; leave the text alone while it is being typed
        fill = self.YELLOW if loaded and not self.lot_size else 'white'
        entry = self.lot_size_entry
        if self.root.focus_get() is not entry:
            entry.configure(state='normal')
            entry.delete(0, tk.END)
            if loaded and self.lot_size:
                entry.insert(0, str(self.lot_size))
        entry.configure(bg=fill, disabledbackground=fill,
                        state='normal' if loaded else 'disabled')

        progress = (self.lot_count_label, self.lot_slash_label, self.lot_bar)
        if loaded and self.lot_size:
            self.lot_count_label.configure(text=str(count))
            for widget in progress:
                widget.grid()
            share = min(count / self.lot_size, 1.0)
            if share > 0:
                self.lot_bar_fill.place(x=0, y=0, relheight=1, relwidth=share)
            else:
                self.lot_bar_fill.place_forget()
        else:
            for widget in progress:
                widget.grid_remove()

    def apply_lot_size(self, event=None):
        """Take the lot size the operator typed, for the open lot and the ones after it."""
        entry = self.lot_size_entry
        if self.lot_part is None or str(entry.cget('state')) == 'disabled':
            return
        text = entry.get().strip()
        if text == str(self.lot_size or ""):
            if event is not None and event.keysym == 'Return':
                self.root.focus_set()
            return

        count = self.open_lot['LT_OK_COUNT'] if self.open_lot else 0
        if not text.isdigit() or int(text) <= 0:
            problem = "Lot size must be a whole number above 0"
        elif int(text) <= count:
            problem = f"Lot size must be more than the {count} cables already in this lot"
        else:
            problem = None

        def restore():
            # Put the size in force back, so the focus-out that follows
            # finds nothing new to apply
            entry.delete(0, tk.END)
            entry.insert(0, str(self.lot_size or ""))
            self.show_lot_progress()

        # Hand focus back so the next barcode scan does not land in this box
        self.root.focus_set()
        if problem:
            self.safe_update_message(problem, "red")
            restore()
            return

        size = int(text)
        if self.open_lot:
            try:
                connection = self.get_database_connection()
                if not connection:
                    restore()
                    return
                cursor = connection.cursor()
                cursor.execute("UPDATE TBL_PART_LOT SET LT_LOT_SIZE = %s WHERE ID = %s",
                               (size, self.open_lot['ID']))
                connection.commit()
                cursor.close()
                connection.close()
                self.open_lot['LT_LOT_SIZE'] = size
            except Exception as e:
                print(f"Error saving lot size: {e}")
                self.safe_update_message(f"Could not save the lot size: {e}", "red")
                restore()
                return

        self.lot_size = size
        self.log_operator_action("LOT_SIZE_SET", f"Lot size {size} from lot {self.lot_number}")
        self.safe_update_message(f"Lot size set to {size} - press START to test", "green")
        self.show_lot_progress()

    def count_ok_in_lot(self, cursor):
        """Count the OK cable just saved into the part's open lot, starting one if needed.

        Runs in save_testing_data()'s transaction. A lot that reaches its
        size closes as FULL, so the next OK cable starts the next lot.
        """
        try:
            now = datetime.now()
            cursor.execute("""
                SELECT ID, LT_OK_COUNT, LT_LOT_SIZE, LT_LOT_NUMBER
                FROM TBL_PART_LOT
                WHERE LT_PART_NUMBER = %s AND LT_STATUS = 'OPEN'
                ORDER BY LT_LOT_NUMBER DESC
                LIMIT 1
            """, (self.partNumber,))
            row = cursor.fetchone()

            if row:
                lot_id, count, size, number = row
                count += 1
                cursor.execute("""
                    UPDATE TBL_PART_LOT
                    SET LT_OK_COUNT = %s, LT_LAST_TRACE_CODE = %s
                    WHERE ID = %s
                """, (count, self.traceabilityCode, lot_id))
            else:
                cursor.execute("""
                    SELECT COALESCE(MAX(LT_LOT_NUMBER), 0) + 1
                    FROM TBL_PART_LOT
                    WHERE LT_PART_NUMBER = %s
                """, (self.partNumber,))
                number = cursor.fetchone()[0]
                count, size = 1, self.lot_size
                cursor.execute("""
                    INSERT INTO TBL_PART_LOT
                    (LT_PART_NUMBER, LT_LOT_NUMBER, LT_LOT_SIZE, LT_OK_COUNT,
                     LT_START_TRACE_CODE, LT_LAST_TRACE_CODE, LT_STATUS, LT_START_DATETIME)
                    VALUES (%s, %s, %s, 1, %s, %s, 'OPEN', %s)
                """, (self.partNumber, number, size, self.traceabilityCode,
                      self.traceabilityCode, now))
                lot_id = cursor.lastrowid

            if size and count >= size:
                cursor.execute("""
                    UPDATE TBL_PART_LOT
                    SET LT_STATUS = 'FULL', LT_END_DATETIME = %s
                    WHERE ID = %s
                """, (now, lot_id))
                self.trace(f"LOT {number} FULL - {count} OK cables")

        except Exception as e:
            print(f"Error counting OK cable into lot: {e}")

    def close_partial_lot(self):
        """Close the part's open lot as PARTIAL now that the part is released."""
        lot = self.open_lot
        if self.lot_part is None or not lot:
            return

        try:
            connection = self.get_database_connection()
            if not connection:
                return
            cursor = connection.cursor()
            cursor.execute("""
                UPDATE TBL_PART_LOT
                SET LT_STATUS = 'PARTIAL', LT_END_DATETIME = %s
                WHERE ID = %s AND LT_STATUS = 'OPEN'
            """, (datetime.now(), lot['ID']))
            connection.commit()
            cursor.close()
            connection.close()

            self.trace(f"LOT {lot['LT_LOT_NUMBER']} PARTIAL - {lot['LT_OK_COUNT']} OK cables")
            self.log_operator_action(
                "LOT_PARTIAL",
                f"Lot {lot['LT_LOT_NUMBER']} closed at {lot['LT_OK_COUNT']} of "
                f"{lot['LT_LOT_SIZE'] or '-'}, {lot['LT_START_TRACE_CODE']} to "
                f"{lot['LT_LAST_TRACE_CODE']}")
        except Exception as e:
            print(f"Error closing partial lot: {e}")

    def load_graph(self):
        """Load today's readings for the current part into the charts."""
        if not self.partNumber:
            return

        try:
            connection = self.get_database_connection()
            if not connection:
                return

            cursor = connection.cursor()
            cursor.execute("""
                SELECT COUNT(*)
                FROM TBL_TEST_DATA
                WHERE TD_PART_NUMBER = %s AND TD_RECORD_DATE = CURDATE()
            """, (self.partNumber,))
            total_records = cursor.fetchone()[0] or 0

            # Only the most recent 1000 readings are plotted.
            cursor.execute("""
                SELECT ID, L1, L2, L3, L4, P1, P2, P3, P4
                FROM TBL_TEST_DATA
                WHERE TD_PART_NUMBER = %s AND TD_RECORD_DATE = CURDATE()
                ORDER BY ID DESC
                LIMIT 1000
            """, (self.partNumber,))
            rows = cursor.fetchall()
            cursor.close()
            connection.close()

            rows.reverse()  # oldest first, so the chart reads left to right

            names = ('L1', 'L2', 'L3', 'L4', 'P1', 'P2', 'P3', 'P4')
            self.chart_series = {name: [] for name in names}
            for row in rows:
                for offset, name in enumerate(names, start=1):
                    self.chart_series[name].append(self.to_chart_value(row[offset]))

            # Keep numbering continuous when older readings were left out.
            self.dataPointX = total_records if total_records > 1000 else len(rows)

            self.redraw_graphs()
            self.load_uph()
            print(f"Loaded {len(rows)} chart points for part: {self.partNumber}")

        except Exception as e:
            print(f"Error loading graph data: {e}")

    def to_chart_value(self, value):
        """Chart values arrive as text or decimals; anything unparsable is a gap."""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def update_charts(self, values=None):
        """Append the readings just measured as the next point on each chart."""
        try:
            if values is None:
                values = {
                    'L1': self.L1MaxValue,
                    'L2': self.L2MaxValue,
                    'L3': self.L3MaxValue,
                    'L4': self.L4MaxValue,
                    'P1': self.P01Value,
                    'P2': self.P02Value,
                    'P3': self.P03Value,
                    'P4': self.P04Value,
                }

            self.dataPointX += 1
            for name, series in self.chart_series.items():
                series.append(self.to_chart_value(values.get(name)))
                # Charts hold the same 1000-point window as the initial load.
                if len(series) > 1000:
                    del series[:len(series) - 1000]

            self.redraw_graphs()
            self.load_uph()
            print(f"Updated charts with data point {self.dataPointX}")

        except Exception as e:
            print(f"Error updating charts: {e}")

    def next_lot_number(self):
        """The lot number the next saved test will get, for showing the operator.

        generate_lot_and_traceability() assigns it when the test is saved,
        counting on from the part's running serial.
        """
        try:
            last = int(self.lotNo or 0)
        except ValueError:
            last = 0
        if datetime.today().date() != self.today:
            last = 0  # the count starts again on a new day
        return f"{last + 1:07d}"

    def get_lot_number(self):
        """Get lot number from database"""
        try:
            connection = self.get_database_connection()
            if not connection:
                return
                
            cursor = connection.cursor(dictionary=True)
            
            lot_query = """
            SELECT RUNNING_LOT_NUMBER
            FROM TBL_PART_RUNNING_SERIAL 
            WHERE PART_NUMBER = %s AND TEST_DAY_DATE = %s
            ORDER BY RUNNING_LOT_NUMBER DESC
            LIMIT 1
            """
            cursor.execute(lot_query, (self.partNumber, datetime.today().date()))
            result = cursor.fetchone()
            self.partRunningSerialExists = result is not None
            last = int(result['RUNNING_LOT_NUMBER'] or 0) if result else 0

            # Tests saved before NG serials were kept can be ahead of the
            # running serial; start after the highest serial used today
            cursor.execute("""
                SELECT MAX(CAST(TD_LOT_NUMBER AS UNSIGNED)) AS LAST_USED
                FROM TBL_TEST_DATA
                WHERE TD_PART_NUMBER = %s AND TD_RECORD_DATE = %s
            """, (self.partNumber, datetime.today().date()))
            used = cursor.fetchone()['LAST_USED']
            self.lotNo = f"{max(last, int(used or 0)):07d}"

            cursor.close()
            connection.close()
            
        except Exception as e:
            print(f"Error getting lot number: {e}")
            self.lotNo = "0"

    def wait_for_pull1_and_start_test(self):
        """
        Wait for PLC to return to pull1 position/state before starting test
        
        Process flow:
        1. Check if PLC is at pull1 state (not at pull1 OK or NG)
        2. Wait until pull1 state is confirmed
        3. Only then start the test process
        4. Provides visual feedback during wait
        """
        try:
            print("🔍 Checking PLC pull1 state before test initiation...")
            
            # Initialize wait counter
            if not hasattr(self, 'pull1_wait_counter'):
                self.pull1_wait_counter = 0
            
            # Check if we have process addresses loaded
            if not hasattr(self, 'process_addresses') or len(self.process_addresses) < 4:
                print("⚠️ Process addresses not loaded - starting test without pull1 check")
                self.start_check_async()
                return
            
            # Use the worker's latest reading. This runs on the Tk thread and
            # repeats every second, so reading the PLC here would stall the
            # window each time the PLC was slow to answer.
            status_values = self.latest_plc_status

            # Get pull1 addresses (index 2 and 3)
            pull1_ok_addr = self.process_addresses[2] if len(self.process_addresses) > 2 else None
            pull1_ng_addr = self.process_addresses[3] if len(self.process_addresses) > 3 else None
            
            # Check if PLC is ready (not at pull1 OK or NG state)
            pull1_ok_state = status_values.get(pull1_ok_addr, False) if pull1_ok_addr else False
            pull1_ng_state = status_values.get(pull1_ng_addr, False) if pull1_ng_addr else False
            
            # Condition: Wait for PLC to be at neutral/ready state (not at pull1 result)
            # The PLC should be ready to start pull1 test
            is_pull1_ready = not pull1_ok_state and not pull1_ng_state
            
            if is_pull1_ready:
                # PLC is at pull1 ready state - can start test
                print("✅ PLC at pull1 ready state - Starting test now")
                if self.lot_size:
                    self.safe_update_message(
                        f"PLC ready at pull1 - Starting test for LOT: {self.next_lot_number()}",
                        "green"
                    )
                else:
                    # START stays refused until there is a lot size
                    self.safe_update_message("Part loaded - enter the lot size, then press START",
                                             "orange")
                self.pull1_wait_counter = 0  # Reset counter
                
                # Reset 1st PULL label color from yellow (waiting) to default blue
                if hasattr(self, 'process_status_labels') and '1st' in self.process_status_labels:
                    self.process_status_labels['1st'].config(bg="#00BFFF")
                
                # Start the test)
                print("🔄 Calling start_check_async() - coil read loop will begin...")
                self.start_check_async()
                
            else:
                # PLC not ready - wait and check again
                self.pull1_wait_counter += 1
                
                # Update status message with wait indication
                wait_msg = f"Waiting for PLC pull1 ready state... ({self.pull1_wait_counter}s)"
                self.safe_update_message(wait_msg, "orange")
                print(f"⏳ {wait_msg}")
                
                # Visual feedback: Set 1st PULL label to yellow (waiting state)
                if hasattr(self, 'process_status_labels') and '1st' in self.process_status_labels:
                    self.process_status_labels['1st'].config(bg="#FFD700")  # Gold/Yellow for waiting
                
                # Print detailed state for debugging
                if self.pull1_wait_counter % 5 == 0:  # Print every 5 seconds
                    print(f"   Pull1 OK: {pull1_ok_state}, Pull1 NG: {pull1_ng_state}")
                
                # Timeout after 60 seconds
                if self.pull1_wait_counter >= 60:
                    print("⚠️ Timeout waiting for pull1 state - Starting test anyway")
                    self.safe_update_message(
                        "Timeout waiting for pull1 - Starting test", 
                        "red"
                    )
                    self.pull1_wait_counter = 0
                    self.start_check_async()
                    return
                
                # Check again after 1 second
                self.root.after(1000, self.wait_for_pull1_and_start_test)
                
        except Exception as e:
            print(f"Error waiting for pull1 state: {e}")
            print("⚠️ Starting test without pull1 check")
            self.start_check_async()
    
    def start_check_async(self):
        """Start asynchronous testing process"""
        # Start the main testing workflow
        print("Starting EOL testing process...")
        self.safe_update_message("Starting test process...", "blue")
        
        # In full implementation, this would start:
        # 1. PLC status coils
        # 2. Part-presence sensors
        # 3. Load cell/position registers
        
        # Check the part on both cameras straight away, while the PLC runs
        # the test
        self.start_camera_checks()

        # Start real PLC monitoring process
        self.root.after(1000, self.start_real_plc_test_process)

    def start_real_plc_test_process(self):
        """
        Start real PLC-controlled testing process
        
        NOTE: PLC writes already done in process_alc_code_cs_style()
              This method ONLY starts the continuous read loops
        """
        try:
            print("🔄 Starting PLC monitoring loops (continuous coil reads)...")
            
            # PLC writes already done during ALC code processing
            # Now we just start the continuous read loops
            
            # Verify PLC connection
            if not hasattr(self, 'plc_client') or not self.plc_client or not self.plc_client.is_socket_open():
                print("⚠️ PLC not connected - cannot start monitoring")
                self.trace("NOT STARTED - the PLC is not connected")
                self.safe_update_message("PLC not connected - cannot start test", "red")
                return
            
            print("✅ PLC connected - starting coil read loop")
            
            # Initialize test state flags. Readings taken while the machine
            # sat idle must not count as this test's peaks.
            self.reset_measurements()
            self.rcvdTestRslt = False  # flag for test completion
            self.noOfValues = 0        # counter for loadcell values
            
            # Start PLC coil monitoring (the coil read step - line 558)
            self.status_monitoring_active = True
            self.start_plc_status_monitoring()
            
            # Start sensor input monitoring (the sensor input step - line 559)
            # This would monitor input sensors if configured
            
            print("✅ Monitoring loops started")
            print("   📖 Monitoring process status coils")
            print("   📖 Reading coils every 200ms")
            print("   ⏳ Waiting for rcvdTestRslt = True")
            
            self.safe_update_message("Test in progress - Monitoring PLC...", "blue")
            
        except Exception as e:
            print(f"Error starting real PLC test process: {e}")
            self.safe_update_message(f"Test Process Error: {e}", "red")

    def start_plc_status_monitoring(self):
        """Start monitoring PLC process status in real-time"""
        try:
            print("Starting PLC status monitoring...")
            self.status_monitoring_active = True
            
            # Start background PLC reading thread
            if not self.plc_thread_running:
                self.plc_thread_running = True
                self.plc_read_thread = threading.Thread(target=self._plc_read_worker, daemon=True)
                self.plc_read_thread.start()
            
            # Start GUI update loop. Only one may run: every test start comes
            # through here, and a second loop would see each result twice.
            if not self.plc_status_loop_running:
                self.plc_status_loop_running = True
                self.monitor_plc_status()
        except Exception as e:
            print(f"Error starting PLC status monitoring: {e}")
    
    def _plc_read_worker(self):
        """Background worker thread for reading PLC status and sensor data - prevents GUI blocking"""
        while self.plc_thread_running and self.status_monitoring_active:
            try:
                # Read PLC coil status in background thread
                status_values = self.read_process_status_values()
                self.latest_plc_status = status_values

                if not status_values:
                    # PLC not answering. Skip the register and sensor reads,
                    # which would each wait out a timeout, and leave the line
                    # free for the window's writes for a while.
                    time.sleep(1)
                    continue

                self.log_process_coil_changes(status_values)
                self.trace_waiting(status_values)

                # Put result in queue (non-blocking)
                try:
                    self.plc_status_queue.put_nowait(status_values)
                except queue.Full:
                    # Queue full, discard oldest value
                    try:
                        self.plc_status_queue.get_nowait()
                        self.plc_status_queue.put_nowait(status_values)
                    except:
                        pass
                
                # Also read input registers for loadcell/pressure data (the input register step)
                self._read_all_input_registers()

                # And the part-presence sensors behind the labels on the image
                self._read_sensor_inputs()

                # Wait 200ms between reads
                time.sleep(0.2)

            except Exception as e:
                print(f"Error in PLC read worker: {e}")
                time.sleep(1)  # Wait longer on error

        # Let start_plc_status_monitoring() start a fresh worker next time.
        self.plc_thread_running = False

    # What each ProcessStatus.txt address is, in file order
    PROCESS_COIL_NAMES = ('AUTO', 'HOME', 'PULL1 OK', 'PULL1 NG', 'PULL2 OK',
                          'PULL2 NG', 'RESULT OK', 'RESULT NG',
                          'CAM1 OK', 'CAM1 NG', 'CAM1 ON')

    def log_process_coil_changes(self, status_values):
        """Print the process coils whenever one changes, so the log shows
        how far the PLC got through a test.

        The Program Select and Machine On coils the console writes are
        read and shown too, so the log says whether they were already on,
        and so is every program in ProgramSelectionInPLC.txt that is on:
        one left on by another part would mean two programs are selected.
        """
        coils = [(name, address, status_values.get(address, False))
                 for name, address in zip(self.PROCESS_COIL_NAMES, self.process_addresses)]
        for name, address in (('PROGRAM SELECT', self.programSelectionPLCAddress),
                              ('MACHINE ON', self.machineOnPLCCoilAddress)):
            if address:
                coils.append((name, address, self.read_coil_state(address)))
        programs_on = self.read_programs_on()

        last = getattr(self, 'last_logged_coils', None)
        if (coils, programs_on) == last:
            return
        if last:
            # Name each coil that changed, as it happens
            before = {(name, address): on for name, address, on in last[0]}
            for name, address, on in coils:
                was = before.get((name, address))
                if was is not None and was != on:
                    self.trace(f"PLC {name} {address}: {'ON' if was else 'off'} -> "
                               f"{'?' if on is None else 'ON' if on else 'off'}")
            if programs_on != last[1]:
                self.trace(f"PLC programs on: {' '.join(last[1] or []) or 'none'} -> "
                           f"{' '.join(programs_on or []) or 'none'}")
        self.last_logged_coils = (coils, programs_on)
        shown = ", ".join(
            f"{name} {address}={'?' if on is None else 'ON' if on else 'off'}"
            for name, address, on in coils)
        programs = ('?' if programs_on is None
                    else ' '.join(programs_on) if programs_on else 'none')
        print(f"PLC coils: {shown}, PROGRAMS ON: {programs}")

    def plc_stage(self, status_values):
        """Where the PLC is in a test, judged from the process coils."""
        on = {name: status_values.get(address, False)
              for name, address in zip(self.PROCESS_COIL_NAMES, self.process_addresses)}
        if on.get('RESULT OK') or on.get('RESULT NG'):
            return "the result is in"
        if on.get('PULL2 OK') or on.get('PULL2 NG'):
            return "2nd pull done, waiting for the result"
        if on.get('PULL1 OK') or on.get('PULL1 NG'):
            return "1st pull done, waiting for the 2nd pull"
        if not on.get('AUTO'):
            return "the PLC is not in AUTO"
        if on.get('HOME'):
            return "the PLC is in AUTO at HOME and has not started the 1st pull"
        return "the PLC is in AUTO away from HOME - a pull may be running"

    def trace_waiting(self, status_values):
        """Every 5 seconds while a part is loaded and no result is in, say
        what the test is waiting for."""
        if not getattr(self, 'current_part_number', None) or self.rcvdTestRslt:
            self._last_wait_trace = 0
            return
        now = time.time()
        if now - getattr(self, '_last_wait_trace', 0) < 5:
            return
        if not getattr(self, '_last_wait_trace', 0):
            self._wait_started = now
        self._last_wait_trace = now
        last = getattr(self, 'last_logged_coils', None)
        programs = ' '.join(last[1]) if last and last[1] else 'none'
        self.trace(f"WAITING {now - self._wait_started:.0f}s: {self.plc_stage(status_values)} "
                   f"| programs on: {programs} | L1 {self.L1MaxValue} P1 {self.P01Value} "
                   f"P2 {self.P02Value}")

    def program_select_addresses(self):
        """Every program-select address in ProgramSelectionInPLC.txt."""
        if getattr(self, '_program_select_addresses', None) is None:
            try:
                with open(data_files.path(data_files.PROGRAM_SELECTION_IN_PLC)) as f:
                    self._program_select_addresses = [
                        a.strip() for a in f.read().split(',') if a.strip()]
            except OSError:
                self._program_select_addresses = []
        return self._program_select_addresses

    def read_programs_on(self):
        """The program-select addresses that are on, read in one request,
        or None if they can't be read."""
        try:
            bits = {address: bit_address(address)
                    for address in self.program_select_addresses()}
        except ValueError:
            return None
        if not bits:
            return []
        first = min(bits.values())
        count = max(bits.values()) - first + 1
        if count > 2000:            # more than one Modbus request can carry
            return None
        try:
            result = self.plc_client.read_coils(first, count=count,
                                                device_id=self.plc_station_id)
            if result.isError():
                return None
            return [address for address, bit in bits.items()
                    if result.bits[bit - first]]
        except Exception:
            return None

    def read_coil_state(self, address):
        """Whether the M coil at address is on, or None if it can't be read."""
        try:
            result = self.plc_client.read_coils(bit_address(address), count=1,
                                                device_id=self.plc_station_id)
            if result.isError():
                return None
            return bool(result.bits[0])
        except Exception:
            return None

    def load_input_sensor_addresses(self):
        """PLC input addresses from InputSensors.txt; label Ln reads the nth one."""
        path = data_files.path(data_files.INPUT_SENSORS)
        try:
            with open(path, 'r') as f:
                return [a.strip() for a in f.read().split(',') if a.strip()]
        except OSError as e:
            print(f"Could not read input sensor addresses: {e}")
            return []

    def _read_sensor_inputs(self):
        """Read the sensor behind each placed label - runs in the worker thread."""
        if not self.plc_client or not self.sensor_label_keys:
            return
        if not self.input_sensor_addresses:
            self.input_sensor_addresses = self.load_input_sensor_addresses()

        states = {}
        for key in self.sensor_label_keys:
            index = int(key[1:]) - 1
            if index >= len(self.input_sensor_addresses):
                continue
            try:
                address = bit_address(self.input_sensor_addresses[index])
                result = self.plc_client.read_discrete_inputs(
                    address, count=1, device_id=self.plc_station_id)
                if not result.isError():
                    states[key] = bool(result.bits[0])
            except Exception as e:
                print(f"Error reading sensor for {key}: {e}")
        self.sensor_states = states

    def apply_sensor_states(self):
        """Show each sensor on its label: its ON text in steady green when made,
        its OFF text blinking when not."""
        for key, is_on in self.sensor_states.items():
            label = self.placed_labels.get(key)
            if label is None:
                continue
            if is_on:
                self.stop_label_blinking(key)
                label.configure(bg="#00FF00", text=label.on_text)
            else:
                label.configure(text=label.off_text)
                if key not in getattr(self, 'blinking_jobs', {}):
                    self.blink_label(label, key)

    def _read_all_input_registers(self):
        """Read all input registers and update internal values - runs in background thread"""
        try:
            if not hasattr(self, 'plc_client') or not self.plc_client:
                return

            # Once the PLC has reported a result the readings are frozen for
            # scoring.
            if self.rcvdTestRslt:
                return

            # Load register addresses if not loaded
            if not hasattr(self, 'hold_register_addresses') or not self.hold_register_addresses:
                self.load_hold_register_addresses()

            if not self.hold_register_addresses:
                return

            station_id = int(config.get('PLC_STATION_ID', '1'))
            register_data = {}
            
            # Track if we should print debug info (every 5 seconds)
            if not hasattr(self, '_last_register_debug_time'):
                self._last_register_debug_time = 0
            
            should_debug = (time.time() - self._last_register_debug_time) > 5
            
            # Read all 8 registers (D001-D008: L1-L4, P1-P4)
            for i, reg_addr_str in enumerate(self.hold_register_addresses):
                if not reg_addr_str.strip():
                    continue
                
                try:
                    # Parse register address
                    if reg_addr_str.startswith('D'):
                        addr_num = int(reg_addr_str[1:])
                    else:
                        addr_num = int(reg_addr_str)
                    
                    # Read input register
                    result = self.plc_client.read_input_registers(
                        address=addr_num,
                        count=1,
                        device_id=station_id
                    )
                    
                    if not result.isError():
                        raw = result.registers[0] if result.registers else 0
                        register_data[reg_addr_str] = raw

                        # Registers hold signed 16-bit values scaled by the
                        # PLC: load in tenths, position in hundredths.
                        signed = raw - 0x10000 if raw >= 0x8000 else raw
                        value = signed / 10.0 if i < 4 else signed / 100.0

                        # Map to device names (L1-L4, P1-P4)
                        if i < 4:
                            device_name = f"L{i+1}"
                            max_attr = f"L{i+1}MaxValue"
                            current_max = getattr(self, max_attr, 0)
                            new_max = max(current_max, value)
                            setattr(self, max_attr, new_max)
                            if should_debug and value > 0:
                                print(f"📊 {device_name}: {value} (Max: {new_max})")
                        elif i < 8:
                            device_name = f"P{i-3}"
                            value_attr = f"P0{i-3}Value"
                            setattr(self, value_attr, value)
                            if should_debug and value > 0:
                                print(f"📊 {device_name}: {value}")
                        
                except Exception as e:
                    # Silently continue on error to avoid flooding logs
                    pass
            
            # Store latest data and update debug time
            if register_data:
                self.latest_register_data = register_data
                if should_debug:
                    self._last_register_debug_time = time.time()
                    print(f"✅ Register data updated: {len(register_data)} registers read")
                
        except Exception as e:
            # Silently handle errors in background thread
            pass
    
    def monitor_plc_status(self):
        """
        Continuously monitor PLC status and update UI
        
        - Simple do-while loop that reads all coils
        - Updates label colors immediately (Lime/Green, OrangeRed/Red, DeepSkyBlue/Blue)
        - Continues until test result is received
        - Uses 200ms delay between iterations (a 200ms pause)
        
        NOTE: PLC reads now happen in background thread to prevent GUI freezing
        """
        try:
            if not hasattr(self, 'status_monitoring_active') or not self.status_monitoring_active:
                self.plc_status_loop_running = False
                return

            # Get PLC status from queue (non-blocking - prevents GUI freeze)
            status_values = {}
            try:
                status_values = self.plc_status_queue.get_nowait()
            except queue.Empty:
                # Nothing new from the worker yet. Reading the PLC from here
                # instead would stall the window behind the worker's request.
                status_values = {}

            self.apply_sensor_states()

            # Update UI labels based on coil values
            if status_values and hasattr(self, 'process_addresses') and self.process_addresses:
                self.update_camera_status(status_values)

                # mapping: processStatusArray indices
                # [0]=AUTO, [1]=HOME, [2]=PULL1_OK, [3]=PULL1_NG, 
                # [4]=PULL2_OK, [5]=PULL2_NG, [6]=TESTRESULT_OK, [7]=TESTRESULT_NG
                
                # AUTO label update
                if len(self.process_addresses) > 0:
                    auto_addr = self.process_addresses[0]
                    auto_result = status_values.get(auto_addr, False)
                    if hasattr(self, 'auto_label'):
                        # Lime if HIGH, DeepSkyBlue if LOW
                        self.auto_label.config(bg="#00FF00" if auto_result else "#00BFFF")
                
                # HOME label update
                if len(self.process_addresses) > 1:
                    home_addr = self.process_addresses[1]
                    home_result = status_values.get(home_addr, False)
                    if hasattr(self, 'home_label'):
                        self.home_label.config(bg="#00FF00" if home_result else "#00BFFF")
                
                # PULL1 label update
                if len(self.process_addresses) > 3:
                    pull1_ok_addr = self.process_addresses[2]
                    pull1_ng_addr = self.process_addresses[3]
                    pull1_ok_result = status_values.get(pull1_ok_addr, False)
                    pull1_ng_result = status_values.get(pull1_ng_addr, False)
                    
                    # Use process_status_labels dict instead (safer)
                    if hasattr(self, 'process_status_labels') and '1st' in self.process_status_labels:
                        label = self.process_status_labels['1st']
                        if pull1_ok_result:
                            label.config(bg="#00FF00")  # Lime
                        elif pull1_ng_result:
                            label.config(bg="#FF4500")  # OrangeRed
                        else:
                            label.config(bg="#00BFFF")  # DeepSkyBlue
                
                # PULL2 label update
                if len(self.process_addresses) > 5:
                    pull2_ok_addr = self.process_addresses[4]
                    pull2_ng_addr = self.process_addresses[5]
                    pull2_ok_result = status_values.get(pull2_ok_addr, False)
                    pull2_ng_result = status_values.get(pull2_ng_addr, False)
                    
                    # Use process_status_labels dict instead (safer)
                    if hasattr(self, 'process_status_labels') and '2nd' in self.process_status_labels:
                        label = self.process_status_labels['2nd']
                        if pull2_ok_result:
                            label.config(bg="#00FF00")  # Lime
                        elif pull2_ng_result:
                            label.config(bg="#FF4500")  # OrangeRed
                        else:
                            label.config(bg="#00BFFF")  # DeepSkyBlue
                
                # TEST RESULT label update
                if len(self.process_addresses) > 7:
                    test_ok_addr = self.process_addresses[6]
                    test_ng_addr = self.process_addresses[7]
                    test_ok_result = status_values.get(test_ok_addr, False)
                    test_ng_result = status_values.get(test_ng_addr, False)
                    
                    if hasattr(self, 'test_label'):
                        if test_ok_result:
                            self.test_label.config(bg="#00FF00")  # Lime
                        elif test_ng_result:
                            self.test_label.config(bg="#FF4500")  # OrangeRed
                        else:
                            self.test_label.config(bg="#00BFFF")  # DeepSkyBlue
                    
                    # Check if test result received. A result coil the PLC
                    # still holds from the test just scored is not a new
                    # result, so wait until both drop before accepting another.
                    if test_ok_result or test_ng_result:
                        if (not getattr(self, 'rcvdTestRslt', False)
                                and not self.awaiting_result_clear):
                            self.rcvdTestRslt = True
                            self.awaiting_result_clear = True
                            print("✅ Test result received from PLC")
                            self.trace(f"PLC RESULT {'OK' if test_ok_result else 'NG'} - "
                                       f"L1 {self.L1MaxValue} P1 {self.P01Value} "
                                       f"P2 {self.P02Value}")
                            # Handle test completion
                            self.root.after(500, self.test_result_command)
                    else:
                        self.awaiting_result_clear = False
            
            # Continue monitoring loop (a 200ms pause)
            if self.status_monitoring_active:
                self.root.after(200, self.monitor_plc_status)
            else:
                self.plc_status_loop_running = False

        except Exception as e:
            print(f"Error in monitor_plc_status: {e}")
            # Continue monitoring despite errors
            if hasattr(self, 'status_monitoring_active') and self.status_monitoring_active:
                self.root.after(200, self.monitor_plc_status)
            else:
                self.plc_status_loop_running = False

    def get_actual_value_for_device(self, device):
        """Get actual value for a specific device"""
        device_map = {
            "L1": self.L1MaxValue,
            "L2": self.L2MaxValue,
            "L3": self.L3MaxValue,
            "L4": self.L4MaxValue,
            "P1": self.P01Value,
            "P2": self.P02Value,
            "P3": self.P03Value,
            "P4": self.P04Value
        }
        return device_map.get(device, 0.0)

    def complete_test_cycle(self):
        """Complete the test cycle and save results"""
        try:
            # Generate lot number and traceability code
            self.generate_lot_and_traceability()
            
            # Check for duplicate traceability code
            if self.check_traceability_duplicate():
                messagebox.showwarning("Duplicate Code", 
                    f"Generated Traceability Code: {self.traceabilityCode} already exists. Saving as 'NG'.")
                self.save_testing_data("NG")
            elif (self.passCounter == len(self.deviceToRead)
                  and self.cam2Result not in ("NG", "ERROR")
                  and not (self.cam1_by_app and self.cam1Result in ("NG", "ERROR"))):
                # All tests passed, and each camera the app checked passed
                # too, or had nothing to check. A camera error fails the part
                # as well: what it looks for was never confirmed.
                self.save_testing_data("OK")
                # Print barcode if configured
                if self.barcodePrintFileName and self.barcodePrintFileName != "NO_BARCODE_PRINT_FILE":
                    self.print_barcode_label_async()
            else:
                # Some tests failed
                self.save_testing_data("NG")
            
            # Update charts and displays
            self.update_charts()
            
        except Exception as e:
            print(f"Error completing test cycle: {e}")

    def generate_lot_and_traceability(self):
        """Generate lot number and traceability code"""
        try:
            # Check if day has changed
            current_date = datetime.today().date()
            if (current_date - self.today).days >= 1:
                self.lotNo = "0"
                self.today = current_date
                self.partRunningSerialExists = False
            
            # Increment lot number
            lot_num = int(self.lotNo) + 1
            self.lotNo = f"{lot_num:07d}"  # 7-digit format
            
            # Generate traceability code: yyMMdd + I + MachineID + G1A + LotNumber
            date_str = current_date.strftime("%y%m%d")
            machine_suffix = self.machineID[2:] if len(self.machineID) > 2 else "01"
            self.traceabilityCode = f"{date_str}I{machine_suffix}G1A{self.lotNo}"
            
            print(f"Generated lot: {self.lotNo}, traceability: {self.traceabilityCode}")
            
        except Exception as e:
            print(f"Error generating lot and traceability: {e}")

    def check_traceability_duplicate(self):
        """Check if traceability code already exists"""
        try:
            connection = self.get_database_connection()
            if not connection:
                return False
                
            cursor = connection.cursor()
            
            duplicate_query = """
            SELECT TD_TRACEABILITY_CODE 
            FROM TBL_TEST_DATA 
            WHERE TD_PART_NUMBER = %s 
            AND TD_RECORD_DATE = %s 
            AND TD_TRACEABILITY_CODE = %s
            """
            cursor.execute(duplicate_query, (self.partNumber, datetime.today().date(), self.traceabilityCode))
            result = cursor.fetchone()
            
            cursor.close()
            connection.close()
            
            return result is not None
            
        except Exception as e:
            print(f"Error checking traceability duplicate: {e}")
            return False

    def reset_dgv_spec_data(self):
        """Blank the measured half of the specification grid.

        The Description/Device/Unit/Min/Max columns describe the part and
        stay put; Actual and Result belong to the test that just ran, so a
        new part must not start out showing the previous one's readings.
        """
        if not hasattr(self, 'spec_tree') or not self.spec_tree:
            return

        try:
            for item in self.spec_tree.get_children():
                values = list(self.spec_tree.item(item, "values"))
                if len(values) >= 7:
                    values[-2] = ""  # Actual
                    values[-1] = ""  # Result
                    self.spec_tree.item(item, values=values, tags=('neutral',))
            self.fit_spec_grid_soon()   # takes the coloured results off
        except Exception as e:
            print(f"Error resetting specification grid: {e}")

    def reset_test_parameters(self):
        """Reset test parameters for next cycle"""
        # Reset message
        self.safe_update_message("", "black")

        # Put the step lamps back to their waiting colour. Without this a
        # finished cycle leaves AUTO/HOME/PULL1/PULL2/TESTRESULT showing the
        # last run's pass and fail colours into the next part.
        self.reset_process_status_labels()

        self.reset_measurements()

        # Reset counters
        self.failCounter = 0
        self.passCounter = 0

        # Reset flags
        self.rcvdTestRslt = False
        self.cam1Result = ""
        if hasattr(self, 'cam1_view'):
            self.show_camera_idle(1)
            self.cam1_view.resume()
        self.cam2Result = ""
        self.cam1Image = self.cam2Image = None
        # The next test checks its part afresh
        self.camera_checks_done = False
        self.after_camera_checks_do = None
        self.cam1_by_app = False
        if hasattr(self, 'cam2_view'):
            self.show_camera_idle(2)
            self.cam2_view.resume()

    def reset_measurements(self):
        """Zero the readings so the next test's peaks start from nothing."""
        # Reset load cell values
        self.loadcell01Value = 0.0
        self.loadcell02Value = 0.0
        self.loadcell03Value = 0.0
        self.loadcell04Value = 0.0
        
        # Reset maximum values
        self.L1MaxValue = 0.0
        self.L2MaxValue = 0.0
        self.L3MaxValue = 0.0
        self.L4MaxValue = 0.0
        
        # Reset pressure values
        self.P01Value = 0.0
        self.P02Value = 0.0
        self.P03Value = 0.0
        self.P04Value = 0.0

    def save_testing_data(self, status):
        """Save testing data to database"""
        try:
            connection = self.get_database_connection()
            if not connection:
                messagebox.showerror("Database Error", "Failed to connect to database for saving results")
                return
                
            cursor = connection.cursor()
            
            # Build dynamic INSERT query based on available columns
            base_columns = [
                'TD_MACHINE_ID', 'TD_PART_NUMBER', 'TD_LOT_NUMBER', 
                'TD_TRACEABILITY_CODE', 'TD_RECORD_DATE', 'TD_DATETIME', 
                'L1', 'P1', 'P2', 'CAM1', 'CAM2', 'CAM1_IMAGE', 'CAM2_IMAGE',
                'TD_OVERALL_STATUS', 'TD_EMP_CODE'
            ]

            base_values = [
                self.machineID, self.partNumber, self.lotNo,
                self.traceabilityCode, datetime.today().date(), datetime.now(),
                self.L1MaxValue, self.P01Value, self.P02Value,
                self.cam1Result, self.cam2Result, self.cam1Image, self.cam2Image,
                status, self.current_employee_id
            ]
            
            # Add optional columns based on part configuration
            if self.columnL2:
                base_columns.append('L2')
                base_values.append(self.L2MaxValue)
            if self.columnL3:
                base_columns.append('L3')
                base_values.append(self.L3MaxValue)
            if self.columnL4:
                base_columns.append('L4')
                base_values.append(self.L4MaxValue)
            if self.columnP3:
                base_columns.append('P3')
                base_values.append(self.P03Value)
            if self.columnP4:
                base_columns.append('P4')
                base_values.append(self.P04Value)
            
            # Create INSERT query
            columns_str = ', '.join(base_columns)
            placeholders = ', '.join(['%s'] * len(base_values))
            
            insert_query = f"""
            INSERT INTO TBL_TEST_DATA ({columns_str})
            VALUES ({placeholders})
            """
            
            cursor.execute(insert_query, base_values)

            # The Data Console reports from TBL_TEST_RESULTS, so the same test
            # goes there too, in the same transaction. Devices this part does
            # not use stay NULL.
            measured = dict(zip(base_columns, base_values))
            devices = ['L1', 'L2', 'L3', 'L4', 'P1', 'P2', 'P3', 'P4']
            cursor.execute("""
                INSERT INTO TBL_TEST_RESULTS
                (LOT_NUMBER, PART_NUMBER, L1, L2, L3, L4, P1, P2, P3, P4,
                 CAM1, CAM2, RESULT, CREATED_BY, EMP_CODE, SPEC_DATA,
                 CAM1_IMAGE, CAM2_IMAGE)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s)
            """, (
                self.lotNo, self.partNumber,
                *(measured.get(d) for d in devices),
                self.cam1Result, self.cam2Result,
                "PASS" if status == "OK" else "NG",
                self.current_employee_id, self.current_employee_id,
                json.dumps({d: measured[d] for d in devices if d in measured}),
                self.cam1Image, self.cam2Image,
            ))
            connection.commit()

            print(f"Test data saved: {status} - Lot: {self.lotNo}, Traceability: {self.traceabilityCode}")
            self.trace(f"SAVED {status} - lot {self.lotNo}, {self.traceabilityCode}")
            self.log_operator_action("TEST_SAVED", f"{status} - lot {self.lotNo}, {self.traceabilityCode}")
            
            # Every saved test used up its serial, NG included, so a reload
            # carries on after it instead of handing the same code out again
            self.update_part_running_serial(cursor)
            if status == "OK":
                self.count_ok_in_lot(cursor)
            connection.commit()
            
            cursor.close()
            connection.close()
            
            # Update display
            self.display_data()
            
        except Exception as e:
            print(f"Error saving testing data: {e}")
            messagebox.showerror("Database Error", f"Failed to save test data: {e}")

    def update_part_running_serial(self, cursor):
        """Update or insert part running serial record"""
        try:
            if not self.partRunningSerialExists:
                # Insert new record
                insert_query = """
                INSERT INTO TBL_PART_RUNNING_SERIAL 
                (PART_NUMBER, TEST_DAY_DATE, TEST_DAY_LAST_DATE_TIME, TRACEABILITY_CODE, RUNNING_LOT_NUMBER)
                VALUES (%s, %s, %s, %s, %s)
                """
                cursor.execute(insert_query, (
                    self.partNumber,
                    datetime.today().date(),
                    datetime.now(),
                    self.traceabilityCode,
                    self.lotNo
                ))
                self.partRunningSerialExists = True
                print("Inserted new part running serial record")
            else:
                # Update existing record
                update_query = """
                UPDATE TBL_PART_RUNNING_SERIAL 
                SET TEST_DAY_LAST_DATE_TIME = %s,
                    TRACEABILITY_CODE = %s,
                    RUNNING_LOT_NUMBER = %s
                WHERE PART_NUMBER = %s AND TEST_DAY_DATE = %s
                """
                cursor.execute(update_query, (
                    datetime.now(),
                    self.traceabilityCode,
                    self.lotNo,
                    self.partNumber,
                    datetime.today().date()
                ))
                print("Updated existing part running serial record")
                
        except Exception as e:
            print(f"Error updating part running serial: {e}")

    def print_barcode_label_async(self):
        """Print barcode label asynchronously"""
        try:
            if not self.prnFileContent:
                print("No barcode template content available")
                return
            
            # Replace placeholders in template
            print_file_text = self.prnFileContent
            
            # Replace all placeholders with actual values
            replacements = {
                # The ALC box is cleared once the part loads, so use the
                # code the part was loaded with.
                '@alcCode@': self.current_alc_code,
                '@partNumber@': self.partNumber,
                '@modelName@': self.modelName,
                '@vendorCode@': self.vendorCode,
                '@eoNumber@': self.eoNumber,
                '@specialData@': self.specialData,
                '@initialID@': self.initialID,
                '@supplierSection@': self.supplierSection,
                '@lotNo@': self.lotNo,
                '@traceabilityCode@': self.traceabilityCode,
                '@L1MaxValue@': f"{self.L1MaxValue:.1f}",
                '@L2MaxValue@': f"{self.L2MaxValue:.1f}",
                '@L3MaxValue@': f"{self.L3MaxValue:.1f}",
                '@L4MaxValue@': f"{self.L4MaxValue:.1f}",
                '@P01Value@': f"+{self.P01Value:.2f}" if self.P01Value >= 0 else f"{self.P01Value:.2f}",
                '@P02Value@': f"+{self.P02Value:.2f}" if self.P02Value >= 0 else f"{self.P02Value:.2f}",
                '@P03Value@': f"+{self.P03Value:.2f}" if self.P03Value >= 0 else f"{self.P03Value:.2f}",
                '@P04Value@': f"+{self.P04Value:.2f}" if self.P04Value >= 0 else f"{self.P04Value:.2f}",
                '@ddMMyy@': datetime.now().strftime("%d%m%y"),
                '@HH:mm:ss@': datetime.now().strftime("%H:%M:%S"),
                '@machineID@': self.machineID,
                '@machineID_NoAlphabet@': self.machineID[2:] if len(self.machineID) > 2 else "01"
            }
            
            for placeholder, value in replacements.items():
                print_file_text = print_file_text.replace(placeholder, str(value))
            
            printer_name = config.get('LABEL_PRINTER_NAME', 'EOL_LABEL_PRNTR')
            send_raw_to_printer(printer_name, print_file_text.encode('utf-8'))
            print(f"Barcode label sent to printer: {printer_name}")

            # Start waiting for barcode scan verification
            self.wait_for_barcode_scan()

        except Exception as e:
            print(f"Error printing barcode label: {e}")
            messagebox.showerror("Print Error", f"Printing failed: {e}")

    def wait_for_barcode_scan(self):
        """Open the scan box for the label just printed and start its timeout."""
        self.printedLabelScanDataInput_Received = False
        self.awaiting_label_scan = True
        self.label_scan_code = self.traceabilityCode

        self.label_scan_entry.configure(state='normal')
        self.label_scan_entry.delete(0, tk.END)
        self.label_scan_entry.focus_set()
        self.safe_update_message("Scan the printed label...", "blue")

        code = self.label_scan_code
        self.root.after(self.printedLabelScanDataInput_WaitTime,
                        lambda: self.check_barcode_scan_timeout(code))

    def check_barcode_scan_timeout(self, code):
        """Record '***' when the label printed for code was never scanned."""
        if self.awaiting_label_scan and self.label_scan_code == code                 and not self.printedLabelScanDataInput_Received:
            self.close_label_scan()
            self.update_scan_result("***", code)
            print("Barcode scan timed out - marked as '***'")

    def on_label_scan_key(self, event=None):
        """Treat the first character as the scan starting.

        Scanners that send no Enter are read once PRINTED_LABEL_SCAN_TIME_INTERVAL
        has passed, which is long enough for the whole code to arrive.
        """
        if (not self.awaiting_label_scan or self.printedLabelScanDataInput_Received
                or not self.label_scan_entry.get()):
            return
        self.printedLabelScanDataInput_Received = True
        self.root.after(self.printedLabelScanDataInput_TimeInterval, self.submit_label_scan)

    def submit_label_scan(self, event=None):
        """Check the scanned label against the code it was printed with."""
        if not self.awaiting_label_scan:
            return "break"
        scanned = self.label_scan_entry.get().strip()
        if not scanned:
            return "break"
        self.printedLabelScanDataInput_Received = True
        code = self.label_scan_code
        self.close_label_scan()
        self.process_barcode_scan_result(scanned, code)
        return "break"

    def close_label_scan(self):
        self.awaiting_label_scan = False
        self.label_scan_entry.delete(0, tk.END)
        self.label_scan_entry.insert(0, self.SCAN_PLACEHOLDER)
        self.label_scan_entry.configure(state='disabled')

    def process_barcode_scan_result(self, scanned_text, code):
        """Record OK when the scanned label carries code, otherwise NG and alert."""
        try:
            if code and code in scanned_text:
                self.update_scan_result("OK", code)
                self.safe_update_message("Label scan OK", "green")
                print("Barcode scan successful - marked as 'OK'")
            else:
                self.update_scan_result("NG", code)
                print("Barcode scan failed - marked as 'NG'")
                
                # Sound the line alert
                self.activate_plc_alert()
                
                messagebox.showwarning(
                    "Scan NG",
                    "Barcode Scan found NG.\nDo NOT fix the Barcode Label to the part.\nPaste it on Production Log Book as NG."
                )
            
        except Exception as e:
            print(f"Error processing barcode scan result: {e}")

    def update_scan_result(self, result, code):
        """Record the scan result against the test whose label carries code."""
        self.log_operator_action("LABEL_SCAN", f"{result} - {code}")
        try:
            connection = self.get_database_connection()
            if not connection:
                return
                
            cursor = connection.cursor()
            
            update_query = """
            UPDATE TBL_TEST_DATA 
            SET TD_BARCODE_SCAN_RESULT = %s
            WHERE TD_PART_NUMBER = %s AND TD_TRACEABILITY_CODE = %s
            """
            cursor.execute(update_query, (result, self.partNumber, code))
            connection.commit()
            
            cursor.close()
            connection.close()
            
            print(f"Barcode scan result updated: {result}")
            
            # The grid and counters both show scan results, so refresh them.
            self.display_data()
            
        except Exception as e:
            print(f"Error updating scan result: {e}")

    def activate_plc_alert(self):
        """Activate PLC alert signal"""
        try:
            if self.alertOnPLCCoilAddress and self.plc_client:
                # M address to its Modbus coil number (see plc_address.py)
                if self.alertOnPLCCoilAddress.startswith('M'):
                    coil_address = bit_address(self.alertOnPLCCoilAddress)
                    
                    # Turn alert ON
                    result = self.plc_client.write_coil(coil_address, True, device_id=self.plc_station_id)
                    if result.isError():
                        print(f"Error activating PLC alert: {result}")
                    else:
                        print(f"PLC alert activated at address {self.alertOnPLCCoilAddress}")
                        
                        # Schedule alert OFF after timeout
                        self.root.after(self.alertOn_TimeInterval, lambda: self.deactivate_plc_alert(coil_address))
                else:
                    messagebox.showerror("Error", f"Alert On PLC Coil Address '{self.alertOnPLCCoilAddress}' "
                                                  "is not an M coil address (e.g. M1001).")
            else:
                messagebox.showerror("Error", "Alert On PLC Coil Address text file is either missing or empty!!")
                
        except Exception as e:
            print(f"Error activating PLC alert: {e}")

    def deactivate_plc_alert(self, coil_address):
        """Deactivate PLC alert signal"""
        try:
            if self.plc_client:
                result = self.plc_client.write_coil(coil_address, False, device_id=self.plc_station_id)
                if result.isError():
                    print(f"Error deactivating PLC alert: {result}")
                else:
                    print(f"PLC alert deactivated at address {coil_address}")
        except Exception as e:
            print(f"Error deactivating PLC alert: {e}")

    def process_alc_code(self, event=None):
        """Process the entered ALC code and retrieve specifications"""
        # Check employee validation first
        if not self.employee_validation_complete or not self.current_employee_id:
            self.safe_update_message("Employee validation required before Part Number entry", "red")
            messagebox.showwarning("Employee Validation Required", 
                                 "Please validate your Employee ID before entering Part Number")
            return
        
        if str(self.alc_entry.cget('state')) != 'normal':
            return      # a part is loaded; its code is only on show

        # Use the value from the entry field
        alc_code = self.alc_entry.get().strip()
        
        if not alc_code or alc_code == self.ALC_PLACEHOLDER:
            messagebox.showwarning("Warning", "Please enter a valid ALC code")
            return
            
        # Log part number entry attempt
        self.log_operator_action("PART_NUMBER_ENTRY", f"ALC Code: {alc_code}", self.current_employee_id)
        
        # Look the part up on the Tk thread: the lookup builds widgets and
        # shows dialogs, which Tkinter only allows from this thread. The code
        # arrives complete on Enter, so there is no settling wait.
        self.process_alc_code_cs_style(alc_code)
        return

    def on_closing(self):
        """Handle window closing event"""
        try:
            # Perform cleanup operations
            self.cleanup()
            
            # Destroy the window
            self.root.destroy()
            
            print("Test console closed properly")
        except Exception as e:
            print(f"Error during window closing: {e}")
            # Ensure window is destroyed even if cleanup fails
            self.root.destroy()

    def reset_process_status_labels(self):
        """Reset all process status labels to default blue state"""
        try:
            # List of status labels to reset
            status_labels = ['auto', 'home', '1st', '2nd', 'test']
            
            for label_name in status_labels:
                label_obj = getattr(self, f"{label_name}_label", None)
                if label_obj:
                    label_obj.config(bg="#00BFFF")  # Default blue color
                    print(f"Reset {label_name} label to default blue")
            
        except Exception as e:
            print(f"Error resetting process status labels: {e}")
    
    def update_specification_result(self, device_name, value, result_tag=""):
        """Update specification tree with actual value and result for a specific device"""
        try:
            if not hasattr(self, 'spec_tree') or not self.spec_tree:
                return False
                
            # Rows stay white whatever the result; fit_spec_grid() colours
            # the Result text alone
            if not hasattr(self, 'spec_color_tags_configured'):
                for tag in ('pass', 'ng', 'neutral'):
                    self.spec_tree.tag_configure(tag, background='#FFFFFF')
                self.spec_color_tags_configured = True

            # Loop through all rows in the spec tree
            for item in self.spec_tree.get_children():
                values = list(self.spec_tree.item(item, "values"))
                # Check if the device column matches the requested device
                if len(values) > 1 and values[1] == device_name:
                    # Update the Actual column (second to last column)
                    values[-2] = f"{value}"
                    
                    # Get min/max from columns 3 and 4
                    min_val = values[3] if len(values) > 3 and values[3] != "N/A" else None
                    max_val = values[4] if len(values) > 4 and values[4] != "N/A" else None
                    
                    # Default to no specific tag
                    result_tag = 'neutral'
                    
                    try:
                        # Convert to float for comparison
                        value_float = float(value)
                        min_float = float(min_val) if min_val else None
                        max_float = float(max_val) if max_val else None
                        
                        # Determine PASS/NG based on min/max comparison
                        if (min_float is None or value_float >= min_float) and \
                           (max_float is None or value_float <= max_float):
                            values[-1] = "PASS"
                            result_tag = 'pass'
                        else:
                            values[-1] = "NG"
                            result_tag = 'ng'
                    except (ValueError, TypeError):
                        # If conversion fails, leave result unchanged
                        print(f"Error converting values for comparison: {value}, {min_val}, {max_val}")
                    
                    # Update the tree item with new values
                    self.spec_tree.item(item, values=values)
                    
                    self.spec_tree.item(item, tags=(result_tag,))
                    self.fit_spec_grid_soon()   # colours the new result

                    return True
            
            # Device not found in tree
            print(f"Warning: Device {device_name} not found in specification tree")
            return False
                
        except Exception as e:
            print(f"Error updating specification result: {e}")
            traceback.print_exc()
            return False


def main():
    try:
        root = tk.Tk()
        
            
        # Initialize the application with error handling
        app = EOLTesterGUI(root)
        
        # Window closing is now handled by the class itself
        root.mainloop()
    except Exception as e:
        print(f"Critical error in main: {e}")
        messagebox.showerror("Critical Error", f"Application failed to start: {e}")
        try:
            if 'root' in locals() and root:
                root.destroy()
        except:
            pass


if __name__ == "__main__":
    main()
