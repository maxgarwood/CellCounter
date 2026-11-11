"""Cell size statistics window for the Cell Counter app.

Shows a histogram of cell area (um^2) against number of cells, for the corner
being viewed or for its whole sample, with summary numbers. A sample with few
but large cells looks very different here from one with many small cells,
even if the counts are similar.

Only kept or opened images are shown, not the moving live view: it would
slow live counting and the numbers would jump about.
"""

import csv
import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

import counter

BAR_WIDTHS_UM2 = (2, 5, 10, 20, 50, 100)   # histogram bar widths to choose from, in um^2
BLOCK_AREA_UM2 = (counter.BLOCK * counter.SQUARE_UM) ** 2   # one corner square: 1 mm^2 = 10^6 um^2


class CellStatsDialog(QDialog):
    """Histogram of cell areas. Refreshes itself when the shown image changes."""

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Cell sizes")
        self.resize(640, 620)
        self._last = None             # what was last drawn, to skip needless redraws

        self.scope = QComboBox()
        self.scope.addItems(["This corner", "Whole sample (all corners)"])
        self.scope.currentIndexChanged.connect(lambda _: self.refresh(force=True))
        self.title = QLabel("")
        self.title.setStyleSheet("font-weight: bold; font-size: 14px;")

        self.figure = Figure(figsize=(6, 3.6), layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.summary = QLabel("")
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setWordWrap(True)
        note = QLabel(
            "Areas come from the detected cell outlines (the magenta cell mask), which include the "
            "cell's thin dark rim, so they read a little larger than the cell itself. They're best for "
            "comparing samples. Cells you added by clicking have no measured size and aren't included.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: 11px;")

        save_btn = QPushButton("Save histogram…")
        save_btn.clicked.connect(self.save_histogram)
        export_btn = QPushButton("Export cell sizes (CSV)…")
        export_btn.clicked.connect(self.export_sizes)

        top = QHBoxLayout()
        top.addWidget(QLabel("Show:"))
        top.addWidget(self.scope)
        top.addStretch()
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(save_btn)
        buttons.addWidget(export_btn)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.title)
        layout.addWidget(self.canvas, stretch=1)
        layout.addWidget(self.summary)
        layout.addWidget(note)
        layout.addLayout(buttons)

    # -------- data
    def records(self):
        """The images to describe: (heading, [ImageRecord]), or (message, []) if none."""
        rec = self.main.current
        if rec is None:
            return "Open an image, or keep a corner from the live camera.", []
        if rec is self.main.live_record:
            return ("Live view: press Space to keep this corner, then its cell sizes appear here. "
                    "(Cell sizes aren't shown live, to keep live counting fast.)"), []
        if self.scope.currentIndex() == 0:
            return f"{rec.name} · {rec.corner}", [rec]
        sample = self.main.shown_sample()
        if sample is None:
            return f"{rec.name} · {rec.corner}", [rec]
        return f"Sample: {sample.name} ({len(sample.records)} corners)", list(sample.records)

    def gather(self, records):
        include_pale = self.main.include_pale.isChecked()
        counted = [r for r in records if r.result.block is not None]
        parts = [r.cell_areas(include_pale) for r in counted]
        areas = np.concatenate([a for a, _ in parts]) if parts else np.zeros(0)
        unmeasured = sum(n for _, n in parts)
        return areas, unmeasured, counted

    # -------- display
    def refresh(self, force=False):
        heading, records = self.records()
        areas, unmeasured, counted = self.gather(records)
        key = (heading, tuple(id(r) for r in records), areas.round(3).tobytes(), unmeasured,
               self.main.include_pale.isChecked())
        if key == self._last and not force:
            return
        self._last = key
        self.title.setText(heading)
        self.figure.clear()
        ax = self.figure.add_subplot()
        if len(areas) == 0:
            ax.set_axis_off()
            if records:
                ax.text(0.5, 0.5, "No counted cells with a measured size", ha="center", va="center",
                        transform=ax.transAxes, color="gray")
            self.summary.setText("")
            self.canvas.draw_idle()
            return

        # About 25-60 bars across the range, with a round bar width
        upper = max(float(np.percentile(areas, 99.5)), 1.0)
        width = next((b for b in BAR_WIDTHS_UM2 if upper / b <= 40), BAR_WIDTHS_UM2[-1])
        top = width * (np.ceil(upper / width) + 1)
        bins = np.arange(0, top + width, width)
        shown = np.clip(areas, 0, bins[-1] - 1e-6)          # rare giants go in the last bar
        ax.hist(shown, bins=bins, color="#2a9d4b", edgecolor="white", linewidth=0.5)
        median = float(np.median(areas))
        ax.axvline(median, color="#c03000", linewidth=1.5, linestyle="--")
        ax.text(median, 0.97, f"  median {median:.0f} µm²", color="#c03000", transform=ax.get_xaxis_transform(),
                va="top", fontsize=9)
        ax.set_xlabel("Cell area (µm²)")
        ax.set_ylabel("Number of cells")
        ax.spines[["top", "right"]].set_visible(False)
        secondary = ax.secondary_xaxis("top", functions=(area_to_diameter, diameter_to_area))
        step = 2 if area_to_diameter(top) <= 30 else 5
        secondary.set_xticks([d for d in range(step, 200, step) if diameter_to_area(d) <= top and d >= 4])
        secondary.set_xlabel("Equivalent diameter (µm)", fontsize=9)
        secondary.tick_params(labelsize=8)
        self.canvas.draw_idle()

        corners = max(len(counted), 1)
        total_per_corner = areas.sum() / corners
        mean, sd = areas.mean(), areas.std()
        q1, q3 = np.percentile(areas, [25, 75])
        pale = "pale cells included" if self.main.include_pale.isChecked() else "pale cells not included"
        extra = f" (+{unmeasured} added by hand, not measured)" if unmeasured else ""
        per = " per corner (average)" if corners > 1 else ""
        self.summary.setText(
            f"<table cellspacing=0 cellpadding=2>"
            f"<tr><td>Cells measured</td><td><b>{len(areas)}</b>{extra} · {pale}</td></tr>"
            f"<tr><td>Median area</td><td><b>{median:.0f} µm²</b> (middle half: {q1:.0f}–{q3:.0f} µm²) "
            f"· ≈ {area_to_diameter(median):.1f} µm across</td></tr>"
            f"<tr><td>Mean area</td><td>{mean:.0f} ± {sd:.0f} µm² (± standard deviation)</td></tr>"
            f"<tr><td>Total cell area{per}</td><td><b>{total_per_corner / 1e3:.1f} × 10³ µm²</b> · covers "
            f"{100 * total_per_corner / BLOCK_AREA_UM2:.1f}% of the 1 mm² corner square</td></tr>"
            f"</table>")

    # -------- saving
    def default_path(self, suffix):
        stamp = datetime.date.today().isoformat()
        return str(self.main.export_dir() / f"cell_sizes_{stamp}{suffix}")

    def save_histogram(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save histogram", self.default_path(".png"),
                                              "PNG image (*.png)")
        if path:
            self.figure.savefig(path, dpi=200)
            self.main.statusBar().showMessage(f"Saved histogram to {Path(path).name}")

    def export_sizes(self):
        """One row per measured cell, for the images currently shown."""
        heading, records = self.records()
        include_pale = self.main.include_pale.isChecked()
        if not records:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export cell sizes", self.default_path(".csv"),
                                              "CSV file (*.csv)")
        if not path:
            return
        sample = self.main.shown_sample()
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sample", "image", "corner", "label", "x", "y", "area_um2", "diameter_um"])
            for r in records:
                for p in r.counted_cells(include_pale):
                    if not p.features:
                        continue
                    a = counter.area_um2(p.features[counter.AREA])
                    w.writerow([sample.name if sample else "", r.name, r.result.corner or "", p.label,
                                f"{p.x:.1f}", f"{p.y:.1f}", f"{a:.1f}", f"{area_to_diameter(a):.2f}"])
        self.main.statusBar().showMessage(f"Exported cell sizes to {Path(path).name}")


def area_to_diameter(a):
    """Diameter (um) of a circle with area a (um^2)."""
    return 2 * np.sqrt(np.maximum(a, 0) / np.pi)


def diameter_to_area(d):
    return np.pi * (np.asarray(d) / 2) ** 2
