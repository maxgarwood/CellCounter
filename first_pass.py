"""Batch check: count a set of images and compare with hand counts in Counts.txt.

The counting itself lives in counter.py (shared with the app).
Marked-up pictures are saved to the output/ folder.

Run with:  python first_pass.py 1.jpg 2.jpg ...
"""

import sys
import time
from pathlib import Path

import cv2
import numpy as np

import counter

OUT_DIR = Path("output")


def read_manual_counts(path="Counts.txt"):
    counts = {}
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if "=" in line:
                name, value = line.split("=")
                counts[name.strip()] = int(value)
    return counts


def main(paths):
    manual = read_manual_counts()
    timings = []
    OUT_DIR.mkdir(exist_ok=True)
    print(f"{'image':<8}{'tilt':>7}{'whole':>7}{'block':>7}{'(pale)':>8}{'Fiji':>7}{'diff':>7}")
    for p in paths:
        t0 = time.perf_counter()
        bgr = cv2.imread(str(p))
        load_time = time.perf_counter() - t0
        r = counter.count_image(bgr)
        timings.append({"load JPEG from disk": load_time, **r.timings})

        s = r.spots
        inside = counter.in_block(s[:, counter.X], s[:, counter.Y], s[:, counter.R], r.block)
        inside &= counter.big_enough(s[:, counter.R])
        pale = np.array([lab == counter.PALE for lab in r.labels], dtype=bool)
        block_count, pale_count = int(inside.sum()), int((inside & pale).sum())

        points = [(x, y, rad, lab, False) for (x, y, rad, *_), lab in zip(s, r.labels)]
        title = f"counted: {block_count} ({pale_count} pale)"
        cv2.imwrite(str(OUT_DIR / f"{Path(p).stem}_annotated.jpg"), counter.draw_overlay(r, points, title))

        m = manual.get(Path(p).name)
        diff = f"{block_count - m:+d}" if m is not None else ""
        print(f"{Path(p).name:<8}{r.tilt:>6.2f}°{len(s):>7}{block_count:>7}{pale_count:>8}"
              f"{m if m is not None else '':>7}{diff:>7}")

    print("\nAverage time per image:")
    for step in timings[0]:
        ms = 1000 * np.mean([t[step] for t in timings])
        print(f"  {step:<24}{ms:7.0f} ms")
    print(f"  {'TOTAL':<24}{1000 * np.mean([sum(t.values()) for t in timings]):7.0f} ms")


if __name__ == "__main__":
    main(sys.argv[1:])
