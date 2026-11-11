"""Cell counting engine for hemocytometer images.

Used by both the app (app.py) and the batch checking script (first_pass.py).

Steps:
  1. Shrink the image to working size and straighten it (the grid is often
     tilted slightly).
  2. Find grid lines, and pick the 4x4 block of large squares nearest the
     image centre (one 1 mm corner square of an Improved Neubauer chamber).
  3. Find cells: flatten the uneven background, then look for small dark,
     sharp-edged spots. Touching cells are split apart. Cells that are much
     fainter than normal are labelled "pale".
  4. Count cells in the block using the usual hemocytometer rule: cells
     touching the top or left border count, cells touching the right or
     bottom border don't.
"""

import time
from dataclasses import dataclass

import cv2
import numpy as np
from scipy.signal import find_peaks
from skimage.feature import peak_local_max

# ---------------------------------------------------------------- settings
WORK_WIDTH = 2000      # images are resized to this width for counting (4000x3000 -> 2000x1500)
CELL_MIN_AREA = 30     # in pixels at working size; smaller spots are treated as noise
CELL_MAX_AREA = 400    # biggest single cell; bigger blobs are split into touching cells if possible
CLUMP_MAX_CELLS = 10   # blobs bigger than this many typical cells are debris, not a clump
CELL_THRESHOLD = 12    # how much darker than background a pixel must be to be "cell"
PALE_BELOW = 50        # cells whose darkest point is fainter than this are "pale"
SHARP_MIN = 8          # spots with blurrier edges than this are out-of-focus debris, not cells
SPLIT_ABOVE = 1.3      # spots bigger than this x a typical cell are checked for touching cells
SQUARE_PX = 312        # side of one large grid square, in pixels at working size
LINE_NOISE = 5         # grid lines must stand out this many times above the image's noise
                       # (tested: 5-6 works; lower picks wrong squares, higher misses lines)
BLOCK = 4              # count a BLOCK x BLOCK group of large squares (4x4 = one 1 mm corner square)

# Chamber geometry, used for cells/mL (assumes an Improved Neubauer chamber)
SQUARE_UM = 250        # side of one large square in micrometres
CHAMBER_DEPTH_MM = 0.1 # depth of the chamber

# Smallest spot that counts as a cell. Changeable at run time (the app has a setting).
MIN_DIAMETER_UM = 6.0
PX_PER_UM = SQUARE_PX / SQUARE_UM   # working-image pixels per micrometre (from the grid: ~1.25)

# Labels a spot can have
CELL, PALE, NOT_CELL = "cell", "pale", "not_cell"

# Columns of the spot table returned by find_spots()
FEATURES = ["x", "y", "radius", "darkness", "sharpness", "area", "aspect"]
X, Y, R, DARK, SHARP, AREA, ASPECT = range(len(FEATURES))


# ---------------------------------------------------------------- image prep

def prepare(img):
    """Resize to working size and convert to greyscale. Returns (colour, grey, scale)."""
    scale = WORK_WIDTH / img.shape[1]
    if abs(scale - 1) > 1e-6:
        method = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=method)
    return img, cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32), scale


def darkness_map(gray):
    """How much darker each pixel is than its surroundings (0 = background)."""
    background = cv2.GaussianBlur(gray, (0, 0), 25)
    return np.clip(background - gray, 0, None)


def rotation_matrix(shape, angle):
    h, w = shape[:2]
    return cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)


def rotate(image, angle):
    h, w = image.shape[:2]
    return cv2.warpAffine(image, rotation_matrix(image.shape, angle), (w, h),
                          flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


# ---------------------------------------------------------------- grid

def line_sharpness(dark):
    """High when grid lines line up exactly with image rows and columns."""
    score = 0
    for axis in (0, 1):
        p = np.median(dark, axis=axis)
        score += np.sort(p - np.median(p))[-8:].sum()
    return score


def find_tilt(dark):
    """Try small rotations and keep the one that makes grid lines sharpest."""
    small = cv2.resize(dark, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)

    def best_of(angles):
        scores = [line_sharpness(rotate(small, a)) for a in angles]
        return float(angles[int(np.argmax(scores))])

    rough = best_of(np.arange(-2, 2.001, 0.25))                      # coarse search
    return best_of(np.arange(rough - 0.2, rough + 0.201, 0.05))      # then fine-tune


def line_profile(green, axis):
    """How strongly a grid line runs along each column (axis=0) or row (axis=1).

    Uses the green channel: the purple grid lines are darkest there, while the
    green cells are fainter. The lines are only ~1 pixel wide and bow slightly,
    so the image is blurred a little across them first. Then the median along
    each column/row ignores cells (even on crowded slides) but keeps lines that
    run the full length. Higher = more line-like.
    """
    # Every other pixel along the lines is plenty, and keeps live counting fast.
    if axis == 0:
        smooth = cv2.GaussianBlur(green[::2], (0, 0), sigmaX=1.0, sigmaY=0.01)
    else:
        smooth = cv2.GaussianBlur(green[:, ::2], (0, 0), sigmaX=0.01, sigmaY=1.0)
    profile = -np.median(smooth, axis=axis)
    return profile - cv2.GaussianBlur(profile.reshape(1, -1), (0, 0), 15).ravel()


def find_lines(profile, min_gap=15):
    """Grid line positions and strengths, from a line_profile.

    A line must stand out LINE_NOISE times above the profile's own noise, so
    the threshold adapts to each image (faint lines, crowded slides). Peaks
    closer than min_gap pixels are merged into one line.
    """
    noise = 1.4826 * np.median(np.abs(profile - np.median(profile)))   # robust standard deviation
    peaks, _ = find_peaks(profile, height=max(LINE_NOISE * noise, 0.3), distance=4)
    groups = []
    for p in peaks:
        if groups and p - groups[-1][-1] < min_gap:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [int(np.mean(g)) for g in groups], [float(profile[g].max()) for g in groups]


def central_block(lines, strengths, size, n=BLOCK, tol=0.08):
    """Choose n large squares in a row whose centre is nearest the image centre.

    Starting from each line, look for a line one large square further on, n
    times. One hidden line (e.g. covered by cells) is allowed, but not at the
    end. Weak stray lines inside are ignored; several strong ones mean this is
    the fine central-square grid, so it's rejected.
    Returns (start, end), or None if there is no block.
    """
    pos, strength = np.asarray(lines), np.asarray(strengths)
    best = None
    for i, start in enumerate(pos):
        found, heights = [start], [strength[i]]
        end, missing = start, 0
        for k in range(n):
            near = np.flatnonzero(np.abs(pos - (end + SQUARE_PX)) < tol * SQUARE_PX)
            if len(near):
                j = near[np.argmax(strength[near])]
                end = pos[j]
                found.append(end)
                heights.append(strength[j])
            else:
                missing += 1
                end += SQUARE_PX
                if missing > 1 or k == n - 1:
                    break
        else:
            inside = (pos > start + 0.15 * SQUARE_PX) & (pos < end - 0.15 * SQUARE_PX) & ~np.isin(pos, found)
            if np.sum(inside & (strength >= 0.4 * np.median(heights))) >= 2:
                continue                                    # fine central grid
            score = (missing, abs((start + end) / 2 - size / 2))
            if best is None or score < best[0]:
                best = (score, int(start), int(end))
    return None if best is None else (best[1], best[2])


def profile_shift(before, after, reach=40):
    """How far a line profile moved between two frames, and how well they match.

    Returns (shift in pixels, match), where match is 1 for an identical
    pattern of grid lines. Moving cells barely change the profiles (they are
    medians), so this measures the slide itself. Rotating or refocusing makes
    the match drop. Shifts beyond `reach` pixels show up as a poor match.
    """
    n = len(before)
    scores = np.empty(2 * reach + 1)
    for i, s in enumerate(range(-reach, reach + 1)):
        a, b = (before[s:], after[:n - s]) if s >= 0 else (before[:n + s], after[-s:])
        a, b = a - a.mean(), b - b.mean()
        scores[i] = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)
    i = int(np.argmax(scores))
    fine = 0.0
    if 0 < i < len(scores) - 1:                       # sub-pixel: fit a parabola to the peak
        left, mid, right = scores[i - 1:i + 2]
        curve = left - 2 * mid + right
        fine = 0.5 * (left - right) / curve if curve else 0.0
    return -(i - reach + fine), float(scores[i])


def which_corner(xs, ys, block):
    """Which corner of the chamber the counting block is, e.g. "top-left".

    The hemocytometer's fine central grid always lies towards the middle of the
    chamber, so if fine lines appear to the right of and below the block, the
    block is the top-left corner. This is the corner as seen in the image;
    microscopes often flip the view, so it may not match the slide itself.
    Unknown parts are shown as "?"; returns None if neither can be told.
    """
    if block is None:
        return None
    x0, y0, x1, y1 = block
    fine_gap = 0.4 * SQUARE_PX

    def fine_side(lines, start, end):
        before = [p for p in lines if start - 2 * SQUARE_PX <= p <= start]
        after = [p for p in lines if end <= p <= end + 2 * SQUARE_PX]
        fine_before = len(before) > 1 and np.min(np.diff(before)) < fine_gap
        fine_after = len(after) > 1 and np.min(np.diff(after)) < fine_gap
        if fine_before != fine_after:
            return "before" if fine_before else "after"
        return None

    col = {"after": "left", "before": "right"}.get(fine_side(xs, x0, x1), "?")
    row = {"after": "top", "before": "bottom"}.get(fine_side(ys, y0, y1), "?")
    return None if col == row == "?" else f"{row}-{col}"


def find_grid(img):
    """Grid lines and the counting block, from the straightened colour image.

    Returns (xs, ys, block, profiles): block is (x0, y0, x1, y1), or None if
    not found; profiles are the column and row line profiles (live mode
    compares them between frames to tell whether the slide has moved).
    """
    green = img[:, :, 1].astype(np.float32)
    px, py = line_profile(green, axis=0), line_profile(green, axis=1)
    xs, x_strength = find_lines(px)
    ys, y_strength = find_lines(py)
    bx = central_block(xs, x_strength, img.shape[1])
    by = central_block(ys, y_strength, img.shape[0])
    block = (bx[0], by[0], bx[1], by[1]) if bx and by else None
    return xs, ys, block, (px, py)


# ---------------------------------------------------------------- cells

def find_spots(dark):
    """Find cell-like spots. Returns (table, typical_radius, mask).

    The table has one row per spot, with the columns listed in FEATURES.
    mask is 1 on the pixels of every blob that became one or more spots
    (the app can show it, to see what the counter detected).
    """
    smooth = cv2.GaussianBlur(dark, (0, 0), 1.5)
    mask = (smooth > CELL_THRESHOLD).astype(np.uint8)
    # Grid lines are long and thin: an "opening" with a small disc removes them
    # while keeping the round cells.
    disc = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, disc)
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    empty = np.zeros((0, len(FEATURES)))
    no_mask = np.zeros(dark.shape, np.uint8)
    if n <= 1:
        return empty, 0.0, no_mask

    area = stats[1:, cv2.CC_STAT_AREA]
    w, h = stats[1:, cv2.CC_STAT_WIDTH], stats[1:, cv2.CC_STAT_HEIGHT]
    aspect = np.maximum(w, h) / np.maximum(np.minimum(w, h), 1)
    keep = (area >= CELL_MIN_AREA) & (area <= CELL_MAX_AREA) & (aspect <= 2.5)
    big = area > CELL_MAX_AREA                  # possibly a clump of touching cells (checked below)

    # Edge sharpness: real cells have crisp edges, out-of-focus debris doesn't
    edges = np.abs(cv2.Laplacian(cv2.GaussianBlur(dark, (0, 0), 1.0), cv2.CV_32F))
    sharpness = np.zeros(n - 1)
    for i in np.flatnonzero(keep | big):
        x, y, bw, bh = stats[i + 1, :4]
        sharpness[i] = edges[y:y + bh, x:x + bw].max()
    keep &= sharpness >= SHARP_MIN
    if not keep.any():
        return empty, 0.0, no_mask

    radius = np.sqrt(area / np.pi)
    typical_area = np.median(area[keep])
    typical_radius = float(np.sqrt(typical_area / np.pi))

    # On crowded slides, neighbouring cells merge into clumps bigger than
    # CELL_MAX_AREA. Split those too, unless too big to be a few cells (debris).
    clump = big & (sharpness >= SHARP_MIN) & (area <= CLUMP_MAX_CELLS * typical_area)

    rows = []
    used = np.zeros(n, np.uint8)            # blobs that became spots (index = blob label)
    for i in np.flatnonzero(keep | clump):
        x, y, bw, bh = stats[i + 1, :4]
        box = labels[y:y + bh, x:x + bw] == i + 1
        darkness = smooth[y:y + bh, x:x + bw][box].max()   # darkest point of the spot
        centres = [centroids[i + 1]]
        if area[i] > SPLIT_ABOVE * typical_area:
            most = max(1, round(area[i] / typical_area))   # a blob can't hold more cells than its size allows
            lobes = find_lobes(box, typical_radius, most)
            if len(lobes) > 1:
                centres = [(x + lx, y + ly) for lx, ly in lobes]
            elif clump[i]:
                continue                        # a big blob that isn't separate cells: debris
        used[i + 1] = 1
        r = radius[i] if len(centres) == 1 else typical_radius
        for (cx, cy), cell_area in zip(centres, split_areas(box, x, y, centres)):
            rows.append((cx, cy, r, darkness, sharpness[i], cell_area, aspect[i]))
    return np.array(rows, dtype=float).reshape(-1, len(FEATURES)), typical_radius, used[labels]


def split_areas(box, x, y, centres):
    """Area of each cell in a blob: every blob pixel belongs to the nearest centre."""
    if len(centres) == 1:
        return [float(box.sum())]
    py, px = np.nonzero(box)
    c = np.asarray(centres) - (x, y)
    nearest = np.argmin((px[:, None] - c[:, 0]) ** 2 + (py[:, None] - c[:, 1]) ** 2, axis=1)
    return np.bincount(nearest, minlength=len(centres)).astype(float).tolist()


def find_lobes(box, typical_radius, most):
    """Split a blob into touching cells.

    Inside the blob, measure each pixel's distance to the edge. A single round
    cell has one peak (its centre); two touching cells have two peaks with a
    "waist" between them. Returns the (x, y) of each peak within the box.
    """
    padded = np.pad(box.astype(np.uint8), 1)
    dist = cv2.distanceTransform(padded, cv2.DIST_L2, 5)
    peaks = peak_local_max(dist, min_distance=max(2, int(0.6 * typical_radius)),
                           threshold_abs=0.5 * typical_radius, exclude_border=False,
                           num_peaks=most)
    return [(px - 1, py - 1) for py, px in peaks]


def auto_labels(spots):
    """Automatic label for each spot: CELL or PALE."""
    return [PALE if d < PALE_BELOW else CELL for d in spots[:, DARK]]


def area_um2(pixels):
    """Area in square micrometres of a spot covering this many working-image pixels."""
    return np.asarray(pixels) / PX_PER_UM ** 2


def diameter_um(r):
    """Approximate diameter in micrometres of a spot with working-image radius r."""
    return 2 * np.asarray(r) / PX_PER_UM


def big_enough(r):
    """True for spots at least MIN_DIAMETER_UM across (works on numbers or arrays)."""
    return diameter_um(r) >= MIN_DIAMETER_UM


def in_block(x, y, r, block):
    """Hemocytometer rule: a cell counts if it lies inside the block or touches
    the top/left border, but not if it touches the right/bottom border.
    Works on single numbers or arrays."""
    if block is None:
        return np.zeros(np.shape(x), dtype=bool)
    x0, y0, x1, y1 = block
    x, y, r = np.asarray(x), np.asarray(y), np.asarray(r)
    return (x + r > x0) & (x + r < x1) & (y + r > y0) & (y + r < y1)


def cells_per_ml(count, dilution=1.0):
    """Concentration from a count over the whole BLOCK x BLOCK area."""
    side_mm = BLOCK * SQUARE_UM / 1000
    volume_ml = side_mm * side_mm * CHAMBER_DEPTH_MM / 1000   # mm^3 -> mL
    return count / volume_ml * dilution


# ---------------------------------------------------------------- whole image

@dataclass
class Result:
    image: np.ndarray       # straightened, working-size colour image (what the user sees)
    tilt: float             # degrees the image was rotated to straighten it
    xs: list                # vertical grid line positions
    ys: list                # horizontal grid line positions
    block: tuple | None     # counting block (x0, y0, x1, y1), or None if not found
    spots: np.ndarray       # spot table, columns as in FEATURES
    labels: list            # automatic label for each spot
    typical_radius: float
    timings: dict           # seconds per step
    corner: str | None = None  # which chamber corner the block is, e.g. "top-left"
    scale: float = 0.5      # working size / original size
    profiles: tuple | None = None  # grid line profiles (columns, rows); see find_grid
    mask: np.ndarray | None = None  # 1 on detected cell pixels (see find_spots)

    def to_original(self, x, y):
        """Convert working-image coordinates back to the original full-size photo."""
        inverse = cv2.invertAffineTransform(rotation_matrix(self.image.shape, self.tilt))
        ox, oy = inverse @ np.array([x, y, 1.0])
        return ox / self.scale, oy / self.scale


def count_image(bgr, tilt=None):
    """Run the full counting method on one colour image (any size).

    If `tilt` is given, the slow search for the grid's tilt is skipped. Live
    counting uses this: the tilt stays the same from frame to frame.
    """
    t = {}
    t0 = time.perf_counter()
    img, gray, scale = prepare(bgr)
    t["shrink + greyscale"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    if tilt is None:
        tilt = find_tilt(darkness_map(gray))
    if abs(tilt) > 0.01:
        img, gray = rotate(img, tilt), rotate(gray, tilt)
    t["find tilt + straighten"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    dark = darkness_map(gray)
    t["flatten background"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    xs, ys, block, profiles = find_grid(img)
    t["find grid + block"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    spots, typical_radius, mask = find_spots(dark)
    labels = auto_labels(spots)
    t["find + count cells"] = time.perf_counter() - t0

    corner = which_corner(xs, ys, block)
    return Result(img, tilt, xs, ys, block, spots, labels, typical_radius, t, corner, scale, profiles, mask)


# ---------------------------------------------------------------- drawing

# Colours for marked-up pictures (OpenCV uses blue-green-red order)
GREEN, BLUE, YELLOW, RED, GREY, GRID = (0, 200, 0), (255, 80, 0), (0, 200, 255), (0, 0, 255), (150, 150, 150), (255, 150, 0)


MASK_COLOUR = (255, 0, 255)   # magenta (BGR), shading over detected cell pixels
MASK_OPACITY = 0.35


def draw_overlay(result, points, title=None, show_grid=True, show_mask=False, show_circles=True):
    """Draw the counting results onto a copy of the straightened image.

    points: list of (x, y, radius, label, manual) tuples.
    show_mask: shade the pixels the counter detected as cells.
    show_circles: draw a circle (or cross) on each spot.
    """
    out = result.image.copy()
    if show_mask and result.mask is not None:
        on = result.mask > 0
        out[on] = ((1 - MASK_OPACITY) * out[on] + MASK_OPACITY * np.array(MASK_COLOUR)).astype(np.uint8)
    if show_grid:
        for x in result.xs:
            cv2.line(out, (x, 0), (x, out.shape[0]), GRID, 1)
        for y in result.ys:
            cv2.line(out, (0, y), (out.shape[1], y), GRID, 1)
    if result.block:
        x0, y0, x1, y1 = result.block
        cv2.rectangle(out, (x0, y0), (x1, y1), RED, 3)
        # Number the squares 1-16, left to right then top to bottom
        side_x, side_y = (x1 - x0) / BLOCK, (y1 - y0) / BLOCK
        for row in range(BLOCK):
            for col in range(BLOCK):
                cv2.putText(out, str(row * BLOCK + col + 1),
                            (int(x0 + col * side_x + 8), int(y0 + row * side_y + 36)),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.1, RED, 2)
    for x, y, r, label, manual in (points if show_circles else []):
        if not manual and not big_enough(r):
            continue
        c = (int(round(x)), int(round(y)))
        if label == NOT_CELL:
            d = int(r) + 2
            cv2.line(out, (c[0] - d, c[1] - d), (c[0] + d, c[1] + d), GREY, 2)
            cv2.line(out, (c[0] - d, c[1] + d), (c[0] + d, c[1] - d), GREY, 2)
            continue
        inside = in_block(x, y, r, result.block)
        colour = (BLUE if label == PALE else GREEN) if inside else YELLOW
        cv2.circle(out, c, int(r) + 4, colour, 2)
        if manual:
            cv2.drawMarker(out, c, colour, cv2.MARKER_CROSS, 8, 2)
    if title:
        cv2.putText(out, title, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.4, RED, 3)
    return out
