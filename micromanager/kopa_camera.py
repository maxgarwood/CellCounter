"""KoPa DF12 microscope camera for Micro-Manager, via the PyDevice adapter.

The camera streams video over its network cable (RTSP). This script reads that
stream in the background and hands Micro-Manager the newest picture whenever it
asks for one, so Snap, Live and multi-dimensional acquisitions work as with any
other camera.

Setup: see SETUP.md next to this file. In short: Hardware Configuration Wizard
-> PyDevice -> PyHub -> choose this file as the script -> add the "KoPa" camera.

Needs: Python 3.9+, numpy, opencv-python, annotated-types.

Limits (these come from the camera, not this script):
  - Micro-Manager's PyDevice only accepts black-and-white 16-bit images, so the
    colour video is converted to grey (or one colour channel, see "Channel").
    8-bit values 0-255 are stretched to 0-65535.
  - Exposure, gain and white balance are set by the camera itself (in its own
    display software). The "Exposure" setting here does nothing to the camera;
    it only exists because Micro-Manager requires it.
"""

import os
import threading
import time
from enum import Enum
from typing import Annotated

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
import cv2  # noqa: E402  (must come after the setting above)
import numpy as np  # noqa: E402
from annotated_types import Ge, Le  # noqa: E402

# ------------------------------------------------------------------ settings
# Change these if needed, then reload the Micro-Manager configuration.
CAMERA_IP = "192.168.31.10"

# Which stream to use. The small one is much lighter for a tablet.
#   "small": 1600 x 1200   (rtsp://<ip>:554/av0_1)
#   "full":  4000 x 3000   (rtsp://<ip>:554/av0_0)
STREAM = "small"

_STREAMS = {"small": ("av0_1", 1600, 1200), "full": ("av0_0", 4000, 3000)}
_PATH, FULL_W, FULL_H = _STREAMS[STREAM]
STREAM_URL = f"rtsp://{CAMERA_IP}:554/{_PATH}"

OPEN_TIMEOUT_MS = 3000     # give up connecting after this long
READ_TIMEOUT_MS = 2000     # give up waiting for a frame after this long
RECONNECT_AFTER = 2.0      # seconds without frames before reconnecting
SNAP_TIMEOUT = 3.0         # how long read() waits for a fresh frame before giving an error


class Channel(Enum):
    """What to turn the colour video into (Micro-Manager needs one channel)."""
    GREY = 0       # normal brightness
    RED = 1
    GREEN = 2      # often the sharpest/brightest channel on colour cameras
    BLUE = 3


# ------------------------------------------------------------------ stream reader

class _StreamReader:
    """Keeps reading the camera in the background and holds only the newest frame.

    Reading continuously matters: if nobody reads, the video piles up in a
    buffer and later reads return old pictures. Reconnects by itself if the
    stream stops (e.g. the network cable is unplugged and plugged back in).
    """

    def __init__(self, url):
        self.url = url
        self.lock = threading.Condition()
        self.frame = None
        self.frame_time = 0.0
        self.frame_number = 0
        self.connected = False
        self.reconnects = 0
        self.running = True
        self.thread = threading.Thread(target=self._loop, name="KoPa stream", daemon=True)
        self.thread.start()

    def _open(self):
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG,
                               [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
                                cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS])
        if cap.isOpened():
            return cap
        cap.release()
        return None

    def _loop(self):
        cap = None
        last_good = 0.0
        while self.running:
            if cap is None:
                cap = self._open()
                if cap is None:
                    self.connected = False
                    time.sleep(1.0)          # camera not reachable yet; try again shortly
                    continue
                last_good = time.time()
            ok, frame = cap.read()
            if ok:
                last_good = time.time()
                with self.lock:
                    self.frame, self.frame_time = frame, last_good
                    self.frame_number += 1
                    self.connected = True
                    self.lock.notify_all()
            elif time.time() - last_good > RECONNECT_AFTER:
                cap.release()                # stream stalled: start again
                cap = None
                self.connected = False
                self.reconnects += 1
            else:
                time.sleep(0.02)
        if cap is not None:
            cap.release()

    def next_frame(self, timeout):
        """Wait for a frame that arrives after this call (so a snap is never old)."""
        deadline = time.time() + timeout
        with self.lock:
            start = self.frame_number
            while self.frame_number == start:
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.lock.wait(left)
            return self.frame

    def frame_rate(self):
        """Rough frames per second over the last couple of seconds."""
        with self.lock:
            n0, t0 = self.frame_number, time.time()
        time.sleep(0.5)
        with self.lock:
            return (self.frame_number - n0) / (time.time() - t0)

    def close(self):
        self.running = False
        self.thread.join(timeout=OPEN_TIMEOUT_MS / 1000 + READ_TIMEOUT_MS / 1000 + 1)


# ------------------------------------------------------------------ the camera device

class KoPaCamera:
    """KoPa DF12 microscope camera (network video stream)."""

    def __init__(self, url=STREAM_URL):
        self._reader = _StreamReader(url)
        self._exposure_ms = 33.0
        self._channel = Channel.GREY
        self._top, self._left = 0, 0
        self._width, self._height = FULL_W, FULL_H

    # ---- required camera properties -------------------------------------
    @property
    def exposure_ms(self) -> float:
        """Not used by this camera (exposure is set by the camera itself)."""
        return self._exposure_ms

    @exposure_ms.setter
    def exposure_ms(self, value):
        self._exposure_ms = float(value)

    @property
    def top(self) -> Annotated[int, Ge(0), Le(FULL_H - 1)]:
        return self._top

    @top.setter
    def top(self, value):
        self._top = int(min(max(value, 0), FULL_H - 1))

    @property
    def left(self) -> Annotated[int, Ge(0), Le(FULL_W - 1)]:
        return self._left

    @left.setter
    def left(self, value):
        self._left = int(min(max(value, 0), FULL_W - 1))

    @property
    def width(self) -> Annotated[int, Ge(1), Le(FULL_W)]:
        return self._width

    @width.setter
    def width(self, value):
        self._width = int(min(max(value, 1), FULL_W))

    @property
    def height(self) -> Annotated[int, Ge(1), Le(FULL_H)]:
        return self._height

    @height.setter
    def height(self, value):
        self._height = int(min(max(value, 1), FULL_H))

    # ---- extra settings shown in Micro-Manager's Device Property Browser ----
    @property
    def channel(self) -> Channel:
        """Grey, or a single colour channel of the colour video."""
        return self._channel

    @channel.setter
    def channel(self, value):
        self._channel = value if isinstance(value, Channel) else Channel[str(value).upper()]

    @property
    def connected(self) -> bool:
        """Whether pictures are currently arriving from the camera."""
        return self._reader.connected

    @property
    def stream(self) -> str:
        return f"{STREAM_URL} ({FULL_W} x {FULL_H})"

    # ---- required camera methods ------------------------------------------
    def read(self):
        """Return the next picture as a 2-D 16-bit image of size (height, width)."""
        frame = self._reader.next_frame(SNAP_TIMEOUT)
        if frame is None:
            raise RuntimeError(
                f"No picture from the camera at {CAMERA_IP} in the last {SNAP_TIMEOUT:.0f} s. "
                "Check the network cable and that this computer's network adapter is set to "
                "192.168.31.50 (subnet 255.255.255.0).")
        if frame.shape[1] != FULL_W or frame.shape[0] != FULL_H:
            # Wrong stream size (e.g. STREAM setting doesn't match the camera): resize to fit.
            frame = cv2.resize(frame, (FULL_W, FULL_H), interpolation=cv2.INTER_AREA)
        return self._to_16bit(self._crop(frame))

    def busy(self):
        return False

    # ---- helpers ------------------------------------------------------------------
    def _crop(self, frame):
        """Cut out the region of interest; pad with black if it goes past the edge."""
        x0, y0, w, h = self._left, self._top, self._width, self._height
        part = frame[y0:y0 + h, x0:x0 + w]
        if part.shape[0] != h or part.shape[1] != w:
            padded = np.zeros((h, w, frame.shape[2]), frame.dtype)
            padded[:part.shape[0], :part.shape[1]] = part
            part = padded
        return part

    def _to_16bit(self, bgr):
        if self._channel == Channel.GREY:
            one = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        else:
            one = bgr[:, :, {Channel.BLUE: 0, Channel.GREEN: 1, Channel.RED: 2}[self._channel]]
        return np.ascontiguousarray(one, dtype=np.uint16) * np.uint16(257)   # 0-255 -> 0-65535


# Micro-Manager's PyDevice picks up the objects listed here.
devices = {"KoPa": KoPaCamera()}


if __name__ == "__main__":
    # Self-test without Micro-Manager:  python kopa_camera.py
    cam = devices["KoPa"]
    print(f"Connecting to {STREAM_URL} ...")
    try:
        image = cam.read()
    except RuntimeError as e:
        print("FAILED:", e)
        raise SystemExit(1)
    print(f"OK: got a {image.shape[1]} x {image.shape[0]} image, "
          f"{image.dtype}, brightness {int(image.min())}-{int(image.max())}")
    print(f"Frame rate: about {cam._reader.frame_rate():.0f} pictures per second")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kopa_test.png")
    cv2.imwrite(out, (image // 257).astype(np.uint8))
    print(f"Saved a test picture to {out}")
    cam._reader.close()
