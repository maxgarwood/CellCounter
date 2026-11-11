"""Cell Counter: a Mac app for counting cells on hemocytometer images.

Images are grouped into samples (one tube at one dilution). Each image is one
corner of the hemocytometer; a sample's concentration uses the average count
over its corners. Images from the same folder go into the same sample
automatically; right-click images to move them to another sample.

Two ways to count:
  - Open images (button, File menu, or drag files/folders onto the window).
  - Live: watch the microscope camera with a running count, and press Space
    ("Add this corner") to keep the current frame as a corner of the live
    sample. Kept frames are saved to captures/<sample name>/.

Correct mistakes on any kept image by clicking:
  - click a circle to change it: cell -> pale cell -> not a cell -> cell
  - click empty space to add a cell that was missed
  - Cmd+Z to undo

Every correction is saved to the corrections/ folder (one CSV per image).
These files are kept so the counter can later learn from them.

Run with:  python app.py   (or double-click "Cell Counter.app")
Packaged for Windows/Mac with PyInstaller: see BUILD_WINDOWS.md.
Self-check (e.g. after packaging):  python app.py --self-test
"""

import csv
import datetime
import hashlib
import math
import os
import re
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QEvent, QFile, QPointF, QSettings, QStandardPaths, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QImage, QKeySequence, QPainter, QPen, QPixmap, qRgba
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QFrame,
    QGraphicsScene, QGraphicsView, QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMenu,
    QMessageBox, QPushButton, QScrollArea, QSplitter, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

import camera
import counter
from counter import CELL, NOT_CELL, PALE

APP_DIR = Path(__file__).resolve().parent
# A packaged app (PyInstaller) can't keep data inside itself, so it uses
# Documents/Cell Counter. Run from source, data stays in the project folder.
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    DATA_DIR = Path(QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)) / "Cell Counter"
else:
    DATA_DIR = APP_DIR
CORRECTIONS_DIR = DATA_DIR / "corrections"
CAPTURES_DIR = DATA_DIR / "captures"
ERROR_LOG = DATA_DIR / "app_errors.log"
# Keyboard shortcut names as shown in help text (Qt's "Ctrl" is the Cmd key on a Mac)
MAC = sys.platform == "darwin"
CMD, SHIFT = ("Cmd+" if MAC else "Ctrl+"), "Shift+"
TRASH = "Trash" if MAC else "Recycle Bin"
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
NEXT_LABEL = {CELL: PALE, PALE: NOT_CELL, NOT_CELL: CELL}
MAX_CORNERS = 4        # a sample normally has at most this many corners
SPREAD_WARN = 20       # warn if a corner differs from the sample average by more than this %
LIVE_STREAM = camera.MAIN_STREAM  # 4000x3000: full quality for kept corners, still ~10 counts/s
# Slide-movement detection while live, from the grid lines only (swimming cells don't
# move the grid). On a still slide the grid shifts < 0.3 px between frames and the
# line pattern matches ~0.99; moving it by even 1 px, rotating 0.3 deg or refocusing
# is clearly larger.
MOVE_SHIFT_PX = 0.5    # grid shift between frames (working-size pixels) that counts as moving
MOVE_MATCH = 0.85      # grid patterns matching worse than this = slide rotated or refocused
SETTLE_SECONDS = 0.4   # how long the slide must be still before the tilt is re-measured
STALE_SECONDS = 2.0    # no new camera frame for this long = connection lost

# Overlay colours (same scheme as the marked-up pictures)
GREEN, BLUE, YELLOW = QColor(0, 200, 0), QColor(0, 80, 255), QColor(255, 200, 0)
RED, GREY, GRID = QColor(255, 0, 0), QColor(150, 150, 150), QColor(0, 150, 255, 160)


# ---------------------------------------------------------------- data

@dataclass
class Point:
    """One spot on the image: found automatically, or added by the user."""
    x: float
    y: float
    r: float
    auto_label: str | None      # what the counter said; None if the user added it
    label: str                  # current label (after any corrections)
    features: tuple = field(default=())

    @property
    def manual(self):
        return self.auto_label is None

    @property
    def visible(self):
        """Automatic spots smaller than the minimum cell size are ignored."""
        return self.manual or bool(counter.big_enough(self.r))


class ImageRecord:
    """An image (opened, or kept from the live camera), its counting result,
    and the user's corrections. Live preview frames have no path and no
    saved corrections."""

    def __init__(self, path, result):
        self.path = Path(path).resolve() if path else None
        self.result = result
        self.points = [Point(row[counter.X], row[counter.Y], row[counter.R], lab, lab, tuple(row))
                       for row, lab in zip(result.spots, result.labels)]
        self.undo_stack = []
        if self.path:
            self.load_corrections()

    @property
    def name(self):
        return self.path.name if self.path else "live view"

    @property
    def corner(self):
        return self.result.corner or "unknown corner"

    # -------- counting
    def counts(self):
        """(normal cells, pale cells) inside the counting block."""
        normal = pale = 0
        for p in self.points:
            if p.label != NOT_CELL and p.visible and counter.in_block(p.x, p.y, p.r, self.result.block):
                if p.label == PALE:
                    pale += 1
                else:
                    normal += 1
        return normal, pale

    def counted_cells(self, include_pale):
        """The points counted in this corner (same rules as total())."""
        if self.result.block is None:
            return []
        return [p for p in self.points
                if p.visible and counter.in_block(p.x, p.y, p.r, self.result.block)
                and (p.label == CELL or (p.label == PALE and include_pale))]

    def cell_areas(self, include_pale):
        """Areas (um^2) of the counted cells, and how many counted cells have no size
        (cells added by clicking aren't measured)."""
        cells = self.counted_cells(include_pale)
        areas = [counter.area_um2(p.features[counter.AREA]) for p in cells if p.features]
        return np.array(areas, dtype=float), sum(1 for p in cells if not p.features)

    def total(self, include_pale):
        """Cells counted in this corner, or None if no counting block was found."""
        if self.result.block is None:
            return None
        normal, pale = self.counts()
        return normal + (pale if include_pale else 0)

    def n_corrections(self):
        return sum(1 for p in self.points if p.manual or p.label != p.auto_label)

    # -------- editing
    def nearest(self, x, y):
        best, best_d = None, None
        for i, p in enumerate(self.points):
            if not p.visible:
                continue
            d = math.hypot(p.x - x, p.y - y)
            if d <= max(p.r + 5, 9) and (best_d is None or d < best_d):
                best, best_d = i, d
        return best

    def click(self, x, y):
        """Change the spot under (x, y), or add a cell there if there isn't one."""
        self.undo_stack.append([Point(**vars(p)) for p in self.points])
        i = self.nearest(x, y)
        if i is None:
            r = self.result.typical_radius or 7.0
            self.points.append(Point(x, y, r, None, CELL))
        else:
            p = self.points[i]
            p.label = NEXT_LABEL[p.label]
            if p.manual and p.label == NOT_CELL:
                del self.points[i]          # removing a cell you added just deletes it
        self.save_corrections()

    def undo(self):
        if not self.undo_stack:
            return False
        self.points = self.undo_stack.pop()
        self.save_corrections()
        return True

    # -------- saving corrections
    @property
    def corrections_path(self):
        tag = hashlib.sha1(str(self.path).encode()).hexdigest()[:8]
        return CORRECTIONS_DIR / f"{self.path.stem}__{tag}.csv"

    def save_corrections(self):
        if not self.path:
            return
        path = self.corrections_path
        if self.n_corrections() == 0:
            path.unlink(missing_ok=True)
            return
        CORRECTIONS_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["image", "source", "auto_label", "label", "changed", "x", "y",
                        "original_x", "original_y", *counter.FEATURES[2:], "tilt",
                        "min_diameter_um", "saved_at"])
            for p in self.points:
                ox, oy = self.result.to_original(p.x, p.y)
                feats = [f"{v:.3f}" for v in p.features[2:]] if p.features else [f"{p.r:.3f}"] + [""] * 4
                w.writerow([str(self.path), "manual" if p.manual else "auto",
                            p.auto_label or "", p.label, int(p.manual or p.label != p.auto_label),
                            f"{p.x:.1f}", f"{p.y:.1f}", f"{ox:.1f}", f"{oy:.1f}",
                            *feats, f"{self.result.tilt:.2f}", counter.MIN_DIAMETER_UM, now])

    def load_corrections(self):
        """Re-apply corrections saved from an earlier session with this image."""
        path = self.corrections_path
        if not path.exists():
            return
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                x, y = float(row["x"]), float(row["y"])
                if row["source"] == "manual":
                    self.points.append(Point(x, y, float(row["radius"]), None, row["label"]))
                elif row["label"] != row["auto_label"]:
                    i = self.nearest(x, y)
                    if i is not None and not self.points[i].manual:
                        self.points[i].label = row["label"]


class Sample:
    """One tube at one dilution, counted over one or more hemocytometer corners."""

    def __init__(self, name, folder=None):
        self.name = name
        self.folder = folder        # images from this folder join this sample automatically
        self.dilution = 1.0
        self.records = []

    def summary(self, include_pale):
        """Combined result over all corners that could be counted."""
        counted = [(r, r.total(include_pale)) for r in self.records]
        counted = [(r, t) for r, t in counted if t is not None]
        totals = [t for _, t in counted]
        mean = float(np.mean(totals)) if totals else None
        conc = counter.cells_per_ml(mean, self.dilution) if mean is not None else None

        warnings = []
        if len(self.records) > MAX_CORNERS:
            warnings.append(f"{len(self.records)} images – more than the usual {MAX_CORNERS} corners.")
        uncounted = [r.name for r in self.records if r.result.block is None]
        if uncounted:
            warnings.append("No counting block found (left out): " + ", ".join(uncounted))
        repeats = [c for c, n in Counter(r.result.corner for r, _ in counted
                                         if r.result.corner and "?" not in r.result.corner).items() if n > 1]
        if repeats:
            warnings.append("Same corner counted more than once: " + ", ".join(repeats))
        off = []
        if mean:
            for r, t in counted:
                diff = 100 * (t - mean) / mean
                if abs(diff) > SPREAD_WARN:
                    off.append(f"{r.name} ({diff:+.0f}%)")
        if off:
            warnings.append(f"Corners more than {SPREAD_WARN}% from the average – check for uneven loading, "
                            "bubbles or debris: " + ", ".join(off))
        return {"counted": counted, "total": sum(totals), "mean": mean, "conc": conc, "warnings": warnings}

    def corners(self):
        return {r.result.corner for r in self.records if r.result.corner}


# ---------------------------------------------------------------- live camera

def slide_moved(before, after):
    """Has the slide moved between two counted frames? Judged from the grid lines only."""
    if before is None or after.profiles is None or before.block is None or after.block is None:
        return False        # no grid to judge by (the "grid lost" rule re-measures the tilt)
    for old, new in zip(before.profiles, after.profiles):
        shift, match = counter.profile_shift(old, new)
        if abs(shift) > MOVE_SHIFT_PX or match < MOVE_MATCH:
            return True
    return False


class LiveWorker(QThread):
    """Reads the camera and counts the newest frame, over and over, in the background.

    The slide is moved by hand, so it can shift and rotate. Each frame's grid
    lines are compared with the previous frame's: while the grid is moving the
    frame is flagged "moving" (cells swimming don't count as movement); once it
    has been still for SETTLE_SECONDS, the slow tilt search runs once, and
    following frames reuse that tilt to keep counting fast.
    """
    counted = Signal(object, object, float, bool)   # Result, its frame, counts per second, slide moving?
    connection = Signal(bool)     # False when frames stop arriving, True when they (re)start
    waiting = Signal(float)       # still trying to make the first connection (seconds so far)
    failed = Signal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        # Connect, retrying until it works or live mode is stopped. When the cable is
        # first plugged in, the USB Ethernet adapter often drops out a few times
        # before settling, so a failed first attempt is normal and not an error.
        cam = None
        waiting_since = time.time()
        while cam is None and not self.isInterruptionRequested():
            try:
                cam = camera.LiveCamera(self.url)
                cam.latest(timeout=8)
            except Exception:
                if cam is not None:
                    cam.close()
                    cam = None
                self.waiting.emit(time.time() - waiting_since)
                for _ in range(10):           # wait ~1 s, but stop promptly if asked
                    if self.isInterruptionRequested():
                        break
                    time.sleep(0.1)
        if cam is None:
            return
        self.connection.emit(True)
        tilt = None                   # tilt measured since the slide last moved (None = needs measuring)
        last_tilt = 0.0               # most recent tilt, used for display while moving
        tilt_time = 0.0
        last_move = 0.0
        previous = None               # Result of the previous frame
        recent = deque(maxlen=15)
        last_count = -1               # frames_read of the last frame counted
        connected = True
        try:
            while not self.isInterruptionRequested():
                # Never count a stale frame: if the stream has stopped (e.g. the
                # network adapter dropped out), report it and wait for the camera
                # to reconnect instead of re-counting the last picture.
                if cam.frame_age() > STALE_SECONDS:
                    if connected:
                        connected = False
                        self.connection.emit(False)
                    time.sleep(0.2)
                    continue
                frame, count = cam.latest_with_count()
                if count == last_count:       # no new frame yet
                    time.sleep(0.005)
                    continue
                last_count = count
                if not connected:
                    connected = True
                    self.connection.emit(True)
                    previous, tilt = None, None   # the slide may have moved meanwhile
                now = time.time()

                # Count quickly with the known tilt, then check whether the grid has moved.
                result = counter.count_image(frame, tilt=last_tilt if tilt is None else tilt)
                if slide_moved(previous, result):
                    last_move = now
                    tilt = None
                moving = now - last_move < SETTLE_SECONDS
                if not moving:
                    if tilt is None:              # settled (or just started): measure the tilt once
                        result = counter.count_image(frame)
                        tilt = last_tilt = result.tilt
                        tilt_time = now
                    elif result.block is None and now - tilt_time > 1.0:
                        tilt = None               # grid lost: re-measure the tilt next frame
                previous = result
                recent.append(time.perf_counter())
                rate = (len(recent) - 1) / (recent[-1] - recent[0]) if len(recent) > 1 else 0.0
                self.counted.emit(result, frame, rate, moving)
        except Exception as e:
            self.failed.emit(f"Live counting stopped: {e}")
        finally:
            cam.close()


# ---------------------------------------------------------------- image view

class ImageView(QGraphicsView):
    """Zoomable, draggable image view that reports clicks (not drags)."""
    clicked = Signal(QPointF)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QColor(40, 40, 40))
        self.setFrameShape(QFrame.NoFrame)
        self._press = None

    def mousePressEvent(self, e):
        self._press = e.position()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if e.button() == Qt.LeftButton and self._press is not None:
            moved = (e.position() - self._press).manhattanLength()
            if moved < 5:
                self.clicked.emit(self.mapToScene(e.position().toPoint()))
        self._press = None

    def zoom(self, factor):
        current = self.transform().m11()
        factor = max(0.05 / current, min(factor, 16 / current))
        self.scale(factor, factor)

    def wheelEvent(self, e):
        # Cmd + scroll zooms; plain scrolling moves around
        if e.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            self.zoom(1.0015 ** e.angleDelta().y())
        else:
            super().wheelEvent(e)

    def viewportEvent(self, e):
        # Trackpad pinch to zoom
        if e.type() == QEvent.NativeGesture and e.gestureType() == Qt.ZoomNativeGesture:
            self.zoom(1 + e.value())
            return True
        return super().viewportEvent(e)

    def fit(self):
        if not self.scene().sceneRect().isEmpty():
            self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)


def to_qimage(bgr):
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()


def pen(colour, width):
    p = QPen(colour, width)
    p.setCosmetic(True)          # stays the same thickness at any zoom
    return p


def millions(value):
    """1270000 -> '1.27 million', to 3 significant figures."""
    if value is None:
        return "–"
    m = value / 1e6
    if m <= 0:
        return "0 million"
    decimals = max(0, 2 - int(math.floor(math.log10(m))))
    return f"{m:.{decimals}f} million"


def scientific(value):
    """1270000 -> '1.27 × 10<sup>6</sup>' (HTML, for a QLabel)"""
    if value is None:
        return "–"
    if value <= 0:
        return "0"
    exp = int(math.floor(math.log10(value)))
    return f"{value / 10 ** exp:.2f} × 10<sup>{exp}</sup>"


def heading(text):
    label = QLabel(text)
    label.setStyleSheet("font-weight: bold; font-size: 15px; margin-top: 6px;")
    return label


def safe_name(text):
    """Make a sample name usable as a folder name."""
    return re.sub(r"[^\w\- ]+", "_", text).strip() or "sample"


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cell Counter")
        self.resize(1450, 920)
        self.setAcceptDrops(True)
        self.settings = QSettings("CellCounter", "CellCounter")
        counter.MIN_DIAMETER_UM = float(self.settings.value("min_diameter_um", counter.MIN_DIAMETER_UM))
        self.samples = []
        self.items = {}              # sample / image -> its row in the sample list
        self.current = None          # the ImageRecord being shown (a kept image or the live view)
        self.overlay = []
        self.pixmap_item = None
        self.live_worker = None
        self.stats_dialog = None      # cell size window, created when first opened
        self.stats_timer = QTimer(self)
        self.stats_timer.setSingleShot(True)
        self.stats_timer.setInterval(300)
        self.stats_timer.timeout.connect(lambda: self.stats_dialog and self.stats_dialog.refresh())
        self.live_record = None      # newest counted live frame
        self.live_frame = None
        self.live_sample = None      # sample that "Add this corner" puts frames into
        self.live_rate = 0.0
        self.live_moving = False
        self.live_lost = False       # camera connection lost (no new frames)
        self.live_waiting = 0.0      # seconds spent waiting for the first connection
        self.live_frame_time = 0.0   # when the newest live frame arrived

        # Left: samples and their images
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.tree_menu)
        self.tree.currentItemChanged.connect(self.on_tree_select)
        self.tree.itemClicked.connect(lambda item, _col: self.on_tree_select(item))
        open_btn = QPushButton("Open images…")
        open_btn.clicked.connect(self.open_dialog)
        tree_hint = QLabel("Images from the same folder form one sample. "
                           "Right-click images to move them to another sample.")
        tree_hint.setWordWrap(True)
        tree_hint.setStyleSheet("color: gray; font-size: 11px;")
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("<b>Samples</b>"))
        lv.addWidget(self.tree)
        lv.addWidget(tree_hint)
        lv.addWidget(open_btn)

        # Centre: the image
        self.view = ImageView()
        self.view.clicked.connect(self.on_click)
        self.pixmap_item = self.view.scene().addPixmap(QPixmap())

        # Right: live controls, then results for this image and its sample
        self.live_btn = QPushButton("Start live camera")
        self.live_btn.setCheckable(True)
        self.live_btn.setStyleSheet("QPushButton { font-weight: bold; padding: 6px; }")
        self.live_btn.clicked.connect(self.toggle_live)
        self.add_corner_btn = QPushButton("Add this corner  (Space)")
        self.add_corner_btn.clicked.connect(self.add_corner)
        self.new_live_sample_btn = QPushButton("New live sample…")
        self.new_live_sample_btn.clicked.connect(self.new_live_sample)
        self.live_status = QLabel("")
        self.live_status.setWordWrap(True)
        self.live_status.setStyleSheet("font-size: 12px;")

        self.image_title = heading("This corner")
        self.count_label = QLabel("–")
        self.count_label.setFont(QFont(self.font().family(), 34, QFont.Bold))
        self.detail_label = QLabel("Open an image or start the live camera")
        self.detail_label.setWordWrap(True)

        self.sample_title = heading("Sample")
        self.dilution = QDoubleSpinBox()
        self.dilution.setRange(0.01, 1e6)
        self.dilution.setDecimals(2)
        self.dilution.setValue(1.0)
        self.dilution.valueChanged.connect(self.on_dilution)
        self.corner_table = QLabel("")
        self.corner_table.setTextFormat(Qt.RichText)
        self.conc_label = QLabel("–")
        self.conc_label.setFont(QFont(self.font().family(), 22, QFont.Bold))
        self.conc_millions = QLabel("")
        self.conc_millions.setFont(QFont(self.font().family(), 15))
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #c03000;")
        chamber_note = QLabel(f"Concentration = average count per corner × 10<sup>4</sup> × dilution. Assumes an Improved "
                              f"Neubauer chamber: each corner is {counter.BLOCK}×{counter.BLOCK} squares of "
                              f"{counter.SQUARE_UM} µm (1 mm²), {counter.CHAMBER_DEPTH_MM} mm deep. Corner names "
                              "are as seen in the image.")
        chamber_note.setWordWrap(True)
        chamber_note.setStyleSheet("color: gray; font-size: 11px;")

        self.include_pale = QCheckBox("Include pale cells in counts")
        self.include_pale.setChecked(self.settings.value("include_pale", True, type=bool))
        self.include_pale.toggled.connect(self.on_include_pale)
        self.min_size = QDoubleSpinBox()
        self.min_size.setRange(1.0, 30.0)
        self.min_size.setSingleStep(0.5)
        self.min_size.setDecimals(1)
        self.min_size.setSuffix(" µm")
        self.min_size.setValue(counter.MIN_DIAMETER_UM)
        self.min_size.setToolTip("Spots smaller than this across are ignored (not drawn, not counted).")
        self.min_size.valueChanged.connect(self.on_min_size)
        self.show_grid = QCheckBox("Show grid lines")
        self.show_grid.setChecked(True)
        self.show_grid.toggled.connect(self.redraw)
        self.show_circles = QCheckBox("Show cell circles")
        self.show_circles.setToolTip("Circle each cell (green, blue, yellow) and mark corrections.")
        self.show_circles.setChecked(self.settings.value("show_circles", True, type=bool))
        self.show_circles.toggled.connect(self.on_show_circles)
        self.show_mask = QCheckBox("Show cell masks")
        self.show_mask.setToolTip("Shade the pixels the counter detected as cells (magenta).")
        self.show_mask.setChecked(self.settings.value("show_mask", False, type=bool))
        self.show_mask.toggled.connect(self.on_show_mask)

        legend = QLabel(
            "<span style='color:#00c800'>&#9679;</span> cell (counted) &nbsp; "
            "<span style='color:#0050ff'>&#9679;</span> pale cell (counted – check it)<br>"
            "<span style='color:#ffc800'>&#9679;</span> outside counting area &nbsp; "
            "<span style='color:#969696'>&#10005;</span> not a cell<br>"
            "<span style='color:#00c800'>&#8853;</span> cell you added &nbsp; "
            "<span style='color:#ff00ff'>&#9632;</span> cell mask (if shown)")
        legend.setStyleSheet("font-size: 12px;")
        help_text = QLabel(
            "<b>Correcting:</b> click a circle to change it (cell → pale → not a cell); "
            f"click empty space to add a missed cell; {CMD}Z undo.<br>"
            "<b>Removing:</b> select images in the list and press Delete; in live mode, Delete removes "
            f"the last corner added. Captured frames go to the {TRASH}.<br>"
            f"<b>Problem frames:</b> {CMD}{SHIFT}S saves the live view to captures/Test frames/ (even with no grid), "
            "without adding it to a sample.<br>"
            f"<b>Moving around:</b> drag to move · pinch or {CMD.rstrip('+')}-scroll to zoom · {CMD}0 fit.")
        help_text.setWordWrap(True)
        help_text.setStyleSheet("font-size: 12px;")

        undo_btn = QPushButton("Undo")
        undo_btn.clicked.connect(self.undo)
        fit_btn = QPushButton("Fit to window")
        fit_btn.clicked.connect(self.view.fit)
        save_img_btn = QPushButton("Save marked-up image…")
        save_img_btn.clicked.connect(self.save_image)
        export_btn = QPushButton("Export results (CSV)…")
        export_btn.clicked.connect(self.export_csv)
        stats_btn = QPushButton("Cell sizes…")
        stats_btn.setToolTip(f"Histogram of cell areas for this corner or its sample ({CMD}I)")
        stats_btn.clicked.connect(self.show_stats)

        panel = QWidget()
        rv = QVBoxLayout(panel)
        rv.addWidget(heading("Live camera"))
        rv.addWidget(self.live_btn)
        row = QHBoxLayout()
        row.addWidget(self.add_corner_btn)
        row.addWidget(self.new_live_sample_btn)
        rv.addLayout(row)
        rv.addWidget(self.live_status)
        rv.addWidget(self.image_title)
        rv.addWidget(self.count_label)
        rv.addWidget(self.detail_label)
        rv.addWidget(self.sample_title)
        row = QHBoxLayout()
        row.addWidget(QLabel("Dilution factor"))
        row.addWidget(self.dilution)
        rv.addLayout(row)
        rv.addWidget(self.corner_table)
        rv.addWidget(QLabel("<b>Concentration</b> (cells / mL)"))
        rv.addWidget(self.conc_label)
        rv.addWidget(self.conc_millions)
        rv.addWidget(self.warning_label)
        rv.addWidget(chamber_note)
        rv.addWidget(heading("Settings"))
        rv.addWidget(self.include_pale)
        row = QHBoxLayout()
        row.addWidget(QLabel("Smallest cell size"))
        row.addWidget(self.min_size)
        rv.addLayout(row)
        rv.addWidget(self.show_grid)
        rv.addWidget(self.show_circles)
        rv.addWidget(self.show_mask)
        rv.addWidget(legend)
        rv.addWidget(help_text)
        rv.addStretch()
        for b in (undo_btn, fit_btn, stats_btn, save_img_btn, export_btn):
            rv.addWidget(b)
        right = QScrollArea()
        right.setWidget(panel)
        right.setWidgetResizable(True)
        right.setFrameShape(QFrame.NoFrame)
        right.setMinimumWidth(340)
        right.setMaximumWidth(420)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self.view)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([250, 830, 370])
        self.setCentralWidget(splitter)
        self.statusBar().showMessage(f"Open images with the button, {CMD}O, or by dragging them onto this window – "
                                     "or start the live camera.")
        self.build_menus()
        self.update_live_controls()
        self.update_panel()

    def build_menus(self):
        def action(menu, text, shortcut, slot):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(shortcut)
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        file_menu = self.menuBar().addMenu("File")
        action(file_menu, "Open Images…", QKeySequence.Open, self.open_dialog)
        action(file_menu, "Save Marked-up Image…", QKeySequence.Save, self.save_image)
        action(file_menu, "Export Results (CSV)…", QKeySequence("Ctrl+E"), self.export_csv)
        edit_menu = self.menuBar().addMenu("Edit")
        action(edit_menu, "Undo", QKeySequence.Undo, self.undo)
        remove = action(edit_menu, "Remove Selected Images", None, self.remove_images)
        remove.setShortcuts([QKeySequence(Qt.Key_Backspace), QKeySequence(Qt.Key_Delete)])
        live_menu = self.menuBar().addMenu("Live")
        action(live_menu, "Start / Stop Live Camera", QKeySequence("Ctrl+L"), self.live_btn.click)
        action(live_menu, "Add This Corner", QKeySequence(Qt.Key_Space), self.add_corner)
        action(live_menu, "New Live Sample…", QKeySequence("Ctrl+Shift+N"), self.new_live_sample)
        live_menu.addSeparator()
        action(live_menu, "Save Frame for Testing", QKeySequence("Ctrl+Shift+S"), self.save_test_frame)
        sample_menu = self.menuBar().addMenu("Sample")
        action(sample_menu, "Move Selected Images to New Sample…", QKeySequence("Ctrl+N"), self.move_to_new_sample)
        action(sample_menu, "Rename Sample…", None, self.rename_sample)
        view_menu = self.menuBar().addMenu("View")
        action(view_menu, "Fit to Window", QKeySequence("Ctrl+0"), self.view.fit)
        action(view_menu, "Cell Sizes…", QKeySequence("Ctrl+I"), self.show_stats)
        action(view_menu, "Zoom In", QKeySequence.ZoomIn, lambda: self.view.zoom(1.25))
        action(view_menu, "Zoom Out", QKeySequence.ZoomOut, lambda: self.view.zoom(0.8))

    # -------- helpers
    @property
    def live(self):
        return self.live_worker is not None

    def all_records(self):
        return [r for s in self.samples for r in s.records]

    def sample_of(self, rec):
        return next((s for s in self.samples if rec in s.records), None)

    def show_stats(self):
        """Open (or bring forward) the cell size statistics window."""
        if self.stats_dialog is None:
            from stats_view import CellStatsDialog     # loads matplotlib only when first needed
            self.stats_dialog = CellStatsDialog(self)
        self.stats_dialog.show()
        self.stats_dialog.raise_()
        self.stats_dialog.refresh(force=True)

    def refresh_stats(self):
        """Update the cell size window shortly after changes (not on every click), so
        correcting cells stays quick."""
        if self.stats_dialog is not None and self.stats_dialog.isVisible():
            self.stats_timer.start()

    def export_dir(self):
        """Default folder for exported files."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR

    def shown_sample(self):
        """The sample whose results are shown on the right."""
        if self.live:
            return self.live_sample
        return self.sample_of(self.current) if self.current else None

    def selected_records(self):
        recs = []
        for item in self.tree.selectedItems():
            kind, obj = item.data(0, Qt.UserRole)
            if kind == "image":
                recs.append(obj)
            else:
                recs += obj.records
        return list(dict.fromkeys(recs))

    # -------- opening images
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.open_paths([u.toLocalFile() for u in e.mimeData().urls()])

    def open_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open images", str(DATA_DIR),
            "Images (*.jpg *.jpeg *.png *.tif *.tiff *.bmp)")
        self.open_paths(paths)

    def sample_for_folder(self, folder):
        sample = next((s for s in self.samples if s.folder == folder), None)
        if sample is None:
            sample = Sample(folder.name, folder)
            self.samples.append(sample)
        return sample

    def open_paths(self, paths):
        files = []
        for p in map(Path, paths):
            if p.is_dir():
                files += sorted(f for f in p.iterdir() if f.suffix.lower() in IMAGE_TYPES)
            elif p.suffix.lower() in IMAGE_TYPES:
                files.append(p)
        already = {r.path: r for r in self.all_records()}
        new = [f for f in files if f.resolve() not in already]
        if not new:
            if files:
                self.stop_live()
                self.select(already[files[0].resolve()])
            return

        self.stop_live()
        added, failed = [], []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for n, f in enumerate(new, 1):
                self.statusBar().showMessage(f"Counting {f.name} ({n} of {len(new)})…")
                QApplication.processEvents()
                bgr = cv2.imread(str(f))
                if bgr is None:
                    failed.append(f.name)
                    continue
                rec = ImageRecord(f, counter.count_image(bgr))
                self.sample_for_folder(rec.path.parent).records.append(rec)
                added.append(rec)
        finally:
            QApplication.restoreOverrideCursor()
        if failed:
            QMessageBox.warning(self, "Couldn't open", "These files couldn't be read:\n" + "\n".join(failed))
        self.rebuild_tree()
        if added:
            self.select(added[0])

    # -------- live camera
    def toggle_live(self):
        if self.live:
            self.stop_live()
        else:
            self.start_live()

    def start_live(self):
        if self.live:
            return
        if self.live_sample is None:
            self.live_sample = self.make_live_sample(f"Live {datetime.datetime.now():%Y-%m-%d %H%M}")
        self.live_worker = LiveWorker(LIVE_STREAM)
        self.live_worker.counted.connect(self.on_live_frame)
        self.live_worker.connection.connect(self.on_live_connection)
        self.live_worker.waiting.connect(self.on_live_waiting)
        self.live_waiting = 0.0
        self.live_lost = False
        self.live_worker.failed.connect(self.on_live_failed)
        self.live_worker.start()
        self.live_record = None
        self.statusBar().showMessage("Connecting to the camera…")
        self.update_live_controls()
        self.update_panel()

    def stop_live(self):
        worker = self.live_worker
        if worker is None:
            return
        self.live_worker = None
        worker.counted.disconnect(self.on_live_frame)
        worker.connection.disconnect(self.on_live_connection)
        worker.waiting.disconnect(self.on_live_waiting)
        self.live_lost = False
        worker.requestInterruption()
        worker.wait(10000)            # connecting can take a few seconds to give up
        if self.current is self.live_record:
            self.current = None
        self.live_record = self.live_frame = None
        self.update_live_controls()
        self.statusBar().showMessage("Live camera stopped.")
        self.update_panel()

    def on_live_failed(self, message):
        if not self.from_current_worker():
            return
        self.stop_live()
        QMessageBox.warning(
            self, "Live camera",
            f"{message}\n\nCheck that the camera's network cable is plugged into the Mac's adapter, and that "
            f"the adapter is set to 192.168.31.50 (System Settings → Network). The camera is at {camera.CAMERA_IP}.")

    def from_current_worker(self):
        """True if the signal being handled came from the current live session.

        Signals cross threads through Qt's event queue, so after a quick stop and
        restart, messages from the previous session can still arrive. They must be
        ignored, or an old picture could be shown (or kept) as if it were live.
        """
        return self.live and self.sender() is self.live_worker

    def on_live_waiting(self, seconds):
        if not self.from_current_worker():
            return
        self.live_waiting = seconds
        self.update_panel()

    def on_live_connection(self, ok):
        if not self.from_current_worker():
            return
        self.live_lost = not ok
        if ok:
            self.statusBar().showMessage("Camera connected." if self.live_record is None else "Camera reconnected.")
        else:
            self.statusBar().showMessage("Camera connection lost – the picture is frozen. Reconnecting… "
                                         "(check the network cable and USB adapter)")
        self.update_panel()

    def on_live_frame(self, result, frame, rate, moving):
        if not self.from_current_worker():
            return
        self.live_frame_time = time.time()
        first = self.live_record is None
        self.live_record = ImageRecord(None, result)
        self.live_frame = frame
        self.live_rate = rate
        self.live_moving = moving
        if first:
            self.statusBar().showMessage(f"Live camera connected ({frame.shape[1]}×{frame.shape[0]}). "
                                         "Press Space to keep the corner in view.")
        self.show_record(self.live_record, fit=first)

    def make_live_sample(self, name):
        """A new sample whose kept frames go into their own folder under captures/."""
        name = name.strip()
        folder = CAPTURES_DIR / safe_name(name)
        n = 2
        while folder.exists() and any(folder.iterdir()):
            folder = CAPTURES_DIR / f"{safe_name(name)} {n}"
            n += 1
        return Sample(name, folder.resolve())

    def new_live_sample(self):
        name, ok = QInputDialog.getText(self, "New live sample", "Name for the new sample:")
        if ok and name.strip():
            self.live_sample = self.make_live_sample(name)
            if not self.live:
                self.start_live()
            self.update_panel()

    def add_corner(self):
        """Keep the current live frame as a corner of the live sample."""
        rec, frame = self.live_record, self.live_frame
        if not self.live or rec is None:
            self.statusBar().showMessage("Start the live camera first, then press Space to add the corner in view.")
            return
        if self.live_lost or time.time() - self.live_frame_time > STALE_SECONDS:
            self.statusBar().showMessage("Can't keep this corner – the camera connection is lost, so the picture "
                                         "is out of date. Wait for it to reconnect.")
            return
        if rec.result.block is None:
            self.statusBar().showMessage("No counting block in view – move to a corner and focus on the grid first.")
            return
        sample = self.live_sample
        sample.folder.mkdir(parents=True, exist_ok=True)
        corner = (rec.result.corner or "corner").replace("?", "x")
        path = sample.folder / f"{corner}_{datetime.datetime.now():%H%M%S}.png"
        cv2.imwrite(str(path), frame)
        kept = ImageRecord(path, rec.result)
        if sample not in self.samples:
            self.samples.append(sample)
        sample.records.append(kept)
        self.rebuild_tree()
        total = kept.total(self.include_pale.isChecked())
        moving_note = (" The slide seemed to be moving – check the picture is sharp."
                       if self.live_moving else "")
        self.statusBar().showMessage(f"Added {kept.corner} ({total} cells) to {sample.name}. "
                                     f"Saved as {path.name}. Click it in the list to check or correct it."
                                     f"{moving_note}")
        self.update_panel()

    def save_test_frame(self):
        """Save the current live frame for testing, even if no grid was found.

        Goes to captures/Test frames/, not into any sample, so problem frames
        (e.g. a crowded slide where the grid isn't found) can be checked later.
        """
        frame, rec = self.live_frame, self.live_record
        if not self.live or frame is None:
            self.statusBar().showMessage("Start the live camera first.")
            return
        if self.live_lost or time.time() - self.live_frame_time > STALE_SECONDS:
            self.statusBar().showMessage("Can't save – the camera connection is lost, so the picture is out of date.")
            return
        folder = CAPTURES_DIR / "Test frames"
        folder.mkdir(parents=True, exist_ok=True)
        note = "grid-found" if rec.result.block else "no-grid"
        path = folder / f"{datetime.datetime.now():%Y-%m-%d_%H%M%S}_{note}.png"
        cv2.imwrite(str(path), frame)
        self.statusBar().showMessage(f"Saved test frame to captures/Test frames/{path.name}")

    def update_live_controls(self):
        self.live_btn.setChecked(self.live)
        self.live_btn.setText("Stop live camera" if self.live else "Start live camera")
        self.add_corner_btn.setEnabled(self.live)


    # -------- sample list
    def rebuild_tree(self):
        """Rebuild the sample list after samples or images were added or moved."""
        self.tree.blockSignals(True)
        self.tree.clear()
        self.items = {}
        bold = QFont(self.font().family(), 13, QFont.Bold)
        for s in self.samples:
            top = QTreeWidgetItem(self.tree)
            top.setData(0, Qt.UserRole, ("sample", s))
            top.setFont(0, bold)
            self.items[id(s)] = top
            for r in s.records:
                child = QTreeWidgetItem(top)
                child.setData(0, Qt.UserRole, ("image", r))
                self.items[id(r)] = child
            top.setExpanded(True)
        if self.current is not None and id(self.current) in self.items and not self.live:
            self.tree.setCurrentItem(self.items[id(self.current)])
        self.tree.blockSignals(False)
        self.refresh_tree_text()

    def refresh_tree_text(self):
        pale = self.include_pale.isChecked()
        for s in self.samples:
            if id(s) not in self.items:
                continue
            summ = s.summary(pale)
            n = len(s.records)
            flag = "  (!)" if summ["warnings"] else ""
            self.items[id(s)].setText(0, f"{s.name}   ({n} corner{'s' if n != 1 else ''}){flag}")
            for r in s.records:
                t = r.total(pale)
                self.items[id(r)].setText(0, f"{r.name}  ·  {r.corner}  ·  {'–' if t is None else t}")

    def select(self, rec):
        item = self.items.get(id(rec))
        if item:
            self.tree.setCurrentItem(item)
            self.on_tree_select(item)

    def on_tree_select(self, item, _previous=None):
        if item is None:
            return
        kind, obj = item.data(0, Qt.UserRole)
        rec = obj if kind == "image" else (obj.records[0] if obj.records else None)
        if rec is None:
            return
        if self.live:
            self.stop_live()              # looking at a kept image ends the live view
        if rec is not self.current:
            self.show_record(rec, fit=True)
        else:
            self.update_panel()

    def tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.tree.setCurrentItem(item)
        kind, obj = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        recs = self.selected_records()
        if kind == "image" or len(self.tree.selectedItems()) > 1:
            menu.addAction(f"Move {len(recs)} image(s) to new sample…", self.move_to_new_sample)
            others = [s for s in self.samples if not all(r in s.records for r in recs)]
            if others:
                sub = menu.addMenu("Move to sample")
                for s in others:
                    sub.addAction(s.name, lambda s=s: self.move_records(recs, s))
            menu.addAction(f"Remove {len(recs)} image(s)", lambda: self.remove_records(recs))
        else:
            menu.addAction("Rename sample…", self.rename_sample)
            menu.addAction("Remove sample", lambda: self.remove_records(list(obj.records)))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def move_records(self, recs, target):
        for r in recs:
            src = self.sample_of(r)
            if src is not target:
                src.records.remove(r)
                target.records.append(r)
        self.samples = [s for s in self.samples if s.records]
        self.rebuild_tree()
        self.update_panel()

    def move_to_new_sample(self):
        recs = self.selected_records()
        if not recs:
            return
        name, ok = QInputDialog.getText(self, "New sample", "Name for the new sample:")
        if ok and name.strip():
            sample = Sample(name.strip())
            self.samples.append(sample)
            self.move_records(recs, sample)

    def rename_sample(self):
        sample = self.shown_sample()
        item = self.tree.currentItem()
        if not self.live and item is not None and item.data(0, Qt.UserRole)[0] == "sample":
            sample = item.data(0, Qt.UserRole)[1]
        if sample is None:
            return
        name, ok = QInputDialog.getText(self, "Rename sample", "Sample name:", text=sample.name)
        if ok and name.strip():
            sample.name = name.strip()
            self.refresh_tree_text()
            self.update_panel()

    def remove_images(self):
        """Delete/Backspace: remove the selected images from the list.

        In live mode nothing is selected, so the most recently added corner is
        removed instead (for a capture that turned out badly).
        """
        if self.live:
            recs = self.live_sample.records[-1:] if self.live_sample else []
            if not recs:
                self.statusBar().showMessage("No corners added to this live sample yet.")
                return
        else:
            recs = [obj for kind, obj in (i.data(0, Qt.UserRole) for i in self.tree.selectedItems())
                    if kind == "image"]
            if not recs:
                self.statusBar().showMessage("Select one or more images in the list, then press Delete to remove them.")
                return
        self.remove_records(recs)

    def remove_records(self, recs):
        """Take images out of the list. Frames captured from the live camera are also
        moved to the Trash (otherwise they'd come back when their sample folder is
        reopened); images opened from elsewhere are left untouched on disk."""
        captures = CAPTURES_DIR.resolve()
        trashed = []
        for r in recs:
            if r.path and captures in r.path.parents and r.path.exists():
                if QFile.moveToTrash(str(r.path)):
                    trashed.append(r.name)
        self.close_records(recs)
        names = ", ".join(r.name for r in recs[:3]) + (f" and {len(recs) - 3} more" if len(recs) > 3 else "")
        note = f" Captured frame(s) moved to the {TRASH}." if trashed else ""
        self.statusBar().showMessage(f"Removed {names}.{note}")

    def close_records(self, recs):
        for r in recs:
            s = self.sample_of(r)
            if s:
                s.records.remove(r)
        self.samples = [s for s in self.samples if s.records]
        if self.current in recs:
            self.current = None
            self.pixmap_item.setPixmap(QPixmap())
            self.clear_overlay()
        self.rebuild_tree()
        remaining = self.all_records()
        if self.current is None and remaining and not self.live:
            self.select(remaining[0])
        self.update_panel()

    # -------- display
    def show_record(self, rec, fit=False):
        self.current = rec
        pixmap = QPixmap.fromImage(to_qimage(rec.result.image))
        self.pixmap_item.setPixmap(pixmap)
        self.view.scene().setSceneRect(pixmap.rect())
        self.redraw()
        if fit:
            self.view.fit()
        if self.live and rec is self.live_record:
            return
        if rec.result.block is None:
            self.statusBar().showMessage(
                f"{rec.name}: couldn't find a complete {counter.BLOCK}×{counter.BLOCK} block of large "
                "squares, so this image can't be counted. Cells are still shown.")
        else:
            n = rec.n_corrections()
            self.statusBar().showMessage(
                f"{rec.name}: {rec.corner} corner, straightened by {rec.result.tilt:+.2f}°."
                + (f" Loaded {n} earlier correction(s)." if n else ""))

    def clear_overlay(self):
        scene = self.view.scene()
        for item in self.overlay:
            scene.removeItem(item)
        self.overlay = []

    def redraw(self):
        """Redraw grid, counting block and cell markers over the image."""
        rec = self.current
        self.clear_overlay()
        if rec is None:
            self.update_panel()
            return
        scene = self.view.scene()
        items = []
        h, w = rec.result.image.shape[:2]
        if self.show_mask.isChecked() and rec.result.mask is not None:
            items.append(self.mask_item(rec.result.mask))
        if self.show_grid.isChecked():
            for x in rec.result.xs:
                items.append(scene.addLine(x, 0, x, h, pen(GRID, 1)))
            for y in rec.result.ys:
                items.append(scene.addLine(0, y, w, y, pen(GRID, 1)))
        block = rec.result.block
        if block:
            x0, y0, x1, y1 = block
            items.append(scene.addRect(x0, y0, x1 - x0, y1 - y0, pen(RED, 3)))
            side_x, side_y = (x1 - x0) / counter.BLOCK, (y1 - y0) / counter.BLOCK
            font = QFont(self.font().family(), 22, QFont.Bold)
            for row in range(counter.BLOCK):
                for col in range(counter.BLOCK):
                    t = scene.addSimpleText(str(row * counter.BLOCK + col + 1), font)
                    t.setBrush(RED)
                    t.setPos(x0 + col * side_x + 8, y0 + row * side_y + 4)
                    items.append(t)
        for p in (rec.points if self.show_circles.isChecked() else []):
            if not p.visible:
                continue
            r = p.r + 4
            if p.label == NOT_CELL:
                d = p.r + 2
                items.append(scene.addLine(p.x - d, p.y - d, p.x + d, p.y + d, pen(GREY, 2)))
                items.append(scene.addLine(p.x - d, p.y + d, p.x + d, p.y - d, pen(GREY, 2)))
                continue
            inside = counter.in_block(p.x, p.y, p.r, block)
            colour = (BLUE if p.label == PALE else GREEN) if inside else YELLOW
            items.append(scene.addEllipse(p.x - r, p.y - r, 2 * r, 2 * r, pen(colour, 2)))
            if p.manual:
                items.append(scene.addLine(p.x - 4, p.y, p.x + 4, p.y, pen(colour, 2)))
                items.append(scene.addLine(p.x, p.y - 4, p.x, p.y + 4, pen(colour, 2)))
        self.overlay = items
        self.update_panel()
        self.refresh_stats()

    def mask_item(self, mask):
        """Translucent magenta shading over the detected cell pixels."""
        # A two-colour palette image (0 = see-through, 1 = magenta) is fast enough for live view
        h, w = mask.shape
        mask = np.ascontiguousarray(mask, dtype=np.uint8)
        image = QImage(mask.data, w, h, w, QImage.Format_Indexed8)
        b, g, r = counter.MASK_COLOUR
        image.setColorTable([qRgba(0, 0, 0, 0), qRgba(r, g, b, 255)])
        item = self.view.scene().addPixmap(QPixmap.fromImage(image))
        item.setOpacity(counter.MASK_OPACITY)
        return item

    def update_panel(self):
        rec = self.current
        pale_on = self.include_pale.isChecked()
        self.refresh_tree_text()

        # Live status line
        if self.live:
            if self.live_record is None:
                if self.live_waiting < 1:
                    self.live_status.setText("Connecting to the camera…")
                else:
                    hint = ("" if self.live_waiting < 15 else
                            "<br>Still nothing – check that the network cable is plugged into the camera and the "
                            f"Mac's USB adapter, and that the adapter is set to 192.168.31.50 "
                            "(System Settings → Network).")
                    self.live_status.setText(
                        f"<span style='color:#d08000'><b>Waiting for the camera…</b> "
                        f"({self.live_waiting:.0f} s)</span> The network adapter can take a few seconds to "
                        f"settle after plugging in – this will connect by itself.{hint}")
            else:
                r = self.live_record.result
                if self.live_lost:
                    where = ("<span style='color:#c03000'><b>Camera connection lost – reconnecting…</b> "
                             "The picture is frozen. Check the network cable and USB adapter.</span>")
                elif self.live_moving:
                    where = ("<span style='color:#d08000'><b>Slide moving…</b> hold still for the best count "
                             "(Space still works)</span>")
                elif r.block:
                    where = f"<b>{r.corner}</b> corner in view"
                else:
                    where = ("<span style='color:#c03000'>No counting block in view – move to a corner "
                             "and focus on the grid.</span>")
                done = self.live_sample.corners() if self.live_sample else set()
                again = (" <span style='color:#c03000'>(already added to this sample)</span>"
                         if r.block and r.corner in done and not self.live_lost else "")
                self.live_status.setText(f"{where}{again}<br>Adding to: <b>{self.live_sample.name}</b>"
                                         f" · {self.live_rate:.1f} counts/s")
        else:
            self.live_status.setText("")

        # This image (or the live view)
        if rec is None:
            self.image_title.setText("This corner")
            self.count_label.setText("–")
            self.detail_label.setText("Open an image or start the live camera")
        else:
            normal, pale = rec.counts()
            total = rec.total(pale_on)
            self.image_title.setText("Live view" if rec is self.live_record else f"This corner: {rec.name}")
            settling = rec is self.live_record and (self.live_moving or self.live_lost)
            self.count_label.setStyleSheet("color: gray;" if settling else "")
            if total is None:
                self.count_label.setText("–")
                self.detail_label.setText("No counting block found.")
            else:
                self.count_label.setText(str(total))
                n = rec.n_corrections()
                self.detail_label.setText(f"{rec.corner} · {normal} normal · {pale} pale"
                                          + (f" · {n} correction{'s' if n != 1 else ''}" if n else ""))

        # Its sample
        sample = self.shown_sample()
        if sample is None:
            self.sample_title.setText("Sample")
            self.corner_table.setText("")
            self.conc_label.setText("–")
            self.conc_millions.setText("")
            self.warning_label.setText("")
            return
        summ = sample.summary(pale_on)
        self.sample_title.setText(f"Sample: {sample.name}")
        self.dilution.blockSignals(True)
        self.dilution.setValue(sample.dilution)
        self.dilution.blockSignals(False)
        rows = []
        for r, t in summ["counted"]:
            diff = f"{100 * (t - summ['mean']) / summ['mean']:+.0f}%" if summ["mean"] else ""
            here = " style='font-weight:bold'" if r is rec else ""
            rows.append(f"<tr{here}><td>{r.corner}</td><td>{r.name}</td>"
                        f"<td align='right'>{t}</td><td align='right' style='color:gray'>{diff}</td></tr>")
        if summ["mean"] is not None:
            rows.append(f"<tr><td colspan=2><b>Average per corner</b></td>"
                        f"<td align='right'><b>{summ['mean']:.1f}</b></td><td></td></tr>")
        elif not sample.records:
            rows.append("<tr><td colspan=4 style='color:gray'>No corners yet – press Space to add the one "
                        "in view.</td></tr>")
        self.corner_table.setText("<table cellspacing=0 cellpadding=2 width='100%'>"
                                  "<tr style='color:gray'><td>Corner</td><td>Image</td>"
                                  "<td align='right'>Cells</td><td align='right'>vs avg</td></tr>"
                                  + "".join(rows) + "</table>")
        self.conc_label.setText(scientific(summ["conc"]))
        self.conc_millions.setText(f"= {millions(summ['conc'])} cells / mL" if summ["conc"] is not None else "")
        self.warning_label.setText("<br>".join("Warning: " + w for w in summ["warnings"]))

    # -------- settings
    def on_include_pale(self, checked):
        self.settings.setValue("include_pale", checked)
        self.update_panel()
        self.refresh_stats()

    def on_show_circles(self, checked):
        self.settings.setValue("show_circles", checked)
        self.redraw()

    def on_show_mask(self, checked):
        self.settings.setValue("show_mask", checked)
        self.redraw()

    def on_min_size(self, value):
        counter.MIN_DIAMETER_UM = value
        self.settings.setValue("min_diameter_um", value)
        self.redraw()

    def on_dilution(self, value):
        sample = self.shown_sample()
        if sample:
            sample.dilution = value
            self.update_panel()

    # -------- editing
    def on_click(self, pos):
        rec = self.current
        if rec is None:
            return
        if rec is self.live_record:
            self.statusBar().showMessage("Live view can't be corrected while it's moving – press Space to keep "
                                         "this corner, then click it in the list to correct it.")
            return
        h, w = rec.result.image.shape[:2]
        if not (0 <= pos.x() < w and 0 <= pos.y() < h):
            return
        if not self.show_circles.isChecked():
            # Corrections would be invisible, so don't make any
            self.statusBar().showMessage("Tick \"Show cell circles\" (Settings) to correct cells by clicking.")
            return
        rec.click(pos.x(), pos.y())
        self.redraw()

    def undo(self):
        if self.current and self.current is not self.live_record and self.current.undo():
            self.redraw()

    # -------- saving
    def save_image(self):
        rec = self.current
        if rec is None:
            return
        stem = rec.path.stem if rec.path else f"live_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        folder = rec.path.parent if rec.path else APP_DIR
        path, _ = QFileDialog.getSaveFileName(self, "Save marked-up image", str(folder / f"{stem}_counted.png"),
                                              "PNG image (*.png)")
        if not path:
            return
        total = rec.total(self.include_pale.isChecked())
        normal, pale = rec.counts()
        title = f"{rec.corner}: {total} cells ({pale} pale)" if total is not None else "no counting block found"
        points = [(p.x, p.y, p.r, p.label, p.manual) for p in rec.points]
        cv2.imwrite(path, counter.draw_overlay(rec.result, points, title, self.show_grid.isChecked(),
                                                     self.show_mask.isChecked(), self.show_circles.isChecked()))
        self.statusBar().showMessage(f"Saved {path}")

    def export_csv(self):
        """Save two CSV files: one row per sample, and one row per corner image."""
        if not self.samples:
            return
        default = str(self.export_dir() / f"cell_counts_{datetime.date.today().isoformat()}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export results", default, "CSV file (*.csv)")
        if not path:
            return
        path = Path(path)
        corners_path = path.with_name(path.stem + "_corners.csv")
        pale_on = self.include_pale.isChecked()
        now = datetime.datetime.now().isoformat(timespec="seconds")

        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sample", "corners_counted", "total_cells", "average_per_corner", "dilution_factor",
                        "cells_per_mL", "pale_included", "min_cell_diameter_um", "warnings", "exported_at"])
            for s in self.samples:
                summ = s.summary(pale_on)
                w.writerow([s.name, len(summ["counted"]), summ["total"],
                            "" if summ["mean"] is None else f"{summ['mean']:.2f}", s.dilution,
                            "" if summ["conc"] is None else f"{summ['conc']:.4g}", pale_on,
                            counter.MIN_DIAMETER_UM, " | ".join(summ["warnings"]), now])

        with open(corners_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sample", "image", "folder", "corner", "cells_counted", "normal_cells", "pale_cells",
                        "pale_included", "median_cell_area_um2", "mean_cell_area_um2", "corrections",
                        "tilt_degrees", "exported_at"])
            for s in self.samples:
                for r in s.records:
                    normal, pale = r.counts()
                    t = r.total(pale_on)
                    areas, _ = r.cell_areas(pale_on)
                    median_area = f"{np.median(areas):.1f}" if len(areas) else ""
                    mean_area = f"{areas.mean():.1f}" if len(areas) else ""
                    w.writerow([s.name, r.name, str(r.path.parent), r.result.corner or "",
                                "" if t is None else t, normal, pale, pale_on, median_area, mean_area,
                                r.n_corrections(),
                                f"{r.result.tilt:.2f}", now])
        self.statusBar().showMessage(f"Exported {len(self.samples)} sample(s) to {path.name} "
                                     f"and the individual corners to {corners_path.name}")

    def closeEvent(self, e):
        self.stop_live()
        super().closeEvent(e)


def log_errors():
    """Write unexpected errors to app_errors.log and show them, instead of the app
    silently closing (a packaged app has no terminal window to print to)."""
    import traceback

    def hook(kind, value, tb):
        text = "".join(traceback.format_exception(kind, value, tb))
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(ERROR_LOG, "a") as f:
                f.write(f"\n--- {datetime.datetime.now().isoformat(timespec='seconds')}\n{text}")
        except OSError:
            pass
        sys.__stderr__ and sys.__stderr__.write(text)
        if QApplication.instance():
            QMessageBox.critical(None, "Cell Counter – unexpected error",
                                 f"Something went wrong:\n\n{value}\n\nDetails were saved to:\n{ERROR_LOG}")
    sys.excepthook = hook


def self_test():
    """Check that everything the app needs works (useful after packaging).
    Prints a report and returns 0 if all is well, 1 if not."""
    report, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            report.append(f"OK    {name}: {fn()}")
        except Exception as e:                     # report every problem, then carry on
            ok = False
            report.append(f"FAIL  {name}: {type(e).__name__}: {e}")

    def count_synthetic():
        img = np.full((1500, 2000, 3), 200, np.uint8)
        for k in range(6):                          # a 5x5 grid of large squares
            p = 200 + k * counter.SQUARE_PX
            cv2.line(img, (p, 0), (p, 1499), (150, 120, 150), 2)
            if p < 1500:
                cv2.line(img, (0, p), (1999, p), (150, 120, 150), 2)
        rng = np.random.default_rng(0)
        for x, y in rng.integers(250, 1400, (40, 2)):
            cv2.circle(img, (int(x), int(y)), 7, (40, 90, 40), -1)
        r = counter.count_image(img)
        return f"grid {'found' if r.block else 'NOT found'}, {len(r.spots)} spots"

    def stats_window():
        import stats_view
        w = MainWindow()
        stats_view.CellStatsDialog(w).refresh(force=True)
        return "opened"

    def camera_stream():
        cam = camera.LiveCamera(camera.SUB_STREAM)
        try:
            frame = cam.latest(timeout=8)
            return f"{frame.shape[1]}x{frame.shape[0]} frame from {camera.CAMERA_IP}"
        finally:
            cam.close()

    app = QApplication.instance() or QApplication(sys.argv)   # noqa: F841 (needed for windows)
    check("Python", lambda: sys.version.split()[0] + (" (packaged)" if FROZEN else " (from source)"))
    def ffmpeg():
        lines = [l for l in cv2.getBuildInformation().splitlines() if l.strip().startswith("FFMPEG:")]
        if not lines or "YES" not in lines[0]:
            raise RuntimeError("OpenCV has no FFmpeg, so the live camera won't work")
        return "yes"

    check("OpenCV video (FFmpeg)", ffmpeg)
    check("Counting", count_synthetic)
    check("Cell sizes window", stats_window)
    check("Data folder", lambda: (DATA_DIR.mkdir(parents=True, exist_ok=True), str(DATA_DIR))[1])
    check("Camera (optional – needs the camera cable)", camera_stream)
    text = "Cell Counter self-test\n" + "\n".join(report)
    if sys.stdout is not None:
        print(text)
    else:                                          # packaged without a console: show it instead
        QMessageBox.information(None, "Cell Counter self-test", text)
    if FROZEN:                                     # keep a copy next to the user's data
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "self_test.txt").write_text(text + "\n")
        except OSError:
            pass
    camera_ok = report[-1].startswith("OK")
    return 0 if ok or (not camera_ok and all(l.startswith("OK") for l in report[:-1])) else 1


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    log_errors()
    app = QApplication(sys.argv)
    app.setApplicationName("Cell Counter")
    window = MainWindow()
    window.show()
    if len(sys.argv) > 1:
        window.open_paths([a for a in sys.argv[1:] if not a.startswith("-")])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
