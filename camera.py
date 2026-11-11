"""Live video from the KoPa DF12 microscope camera.

The camera streams H.264 video over its network cable (RTSP):
  - main stream: 4000 x 3000 at about 29 frames per second
  - sub stream:  1600 x 1200 at about 29 frames per second

Network setup (found 2 Nov 2026):
  - camera address: 192.168.31.10  (fixed; nothing on this network hands out addresses)
  - the lab display (Android) uses 192.168.31.99
  - the Mac's USB Ethernet adapter is set manually to 192.168.31.50, subnet
    255.255.255.0, router left blank (so internet stays on Wi-Fi)

Run this file to grab one frame, save it, and count it:
    python camera.py            (main stream)
    python camera.py sub        (sub stream)
"""

import os
import sys
import threading
import time

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
import cv2  # noqa: E402  (must come after the setting above)

CAMERA_IP = "192.168.31.10"
MAIN_STREAM = f"rtsp://{CAMERA_IP}:554/av0_0"   # 4000 x 3000
SUB_STREAM = f"rtsp://{CAMERA_IP}:554/av0_1"    # 1600 x 1200


OPEN_TIMEOUT_MS = 3000    # give up connecting after this long
READ_TIMEOUT_MS = 2000    # give up waiting for a frame after this long (FFmpeg's default is ~30 s)
RECONNECT_AFTER = 2.0     # seconds without a new frame before reconnecting


def open_stream(url):
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
                                                  cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS])


class LiveCamera:
    """Reads frames in the background and always keeps only the newest one.

    Video arrives at ~29 frames per second; counting may be slower. Keeping
    just the latest frame means the count always reflects what's on the slide
    now, rather than falling further and further behind.

    If the stream stops (e.g. the network adapter drops out), the reader keeps
    trying to reconnect. Use `frame_age()` to tell whether the newest frame is
    actually recent - after a dropout, the last good frame is still held but
    is stale.
    """

    def __init__(self, url=MAIN_STREAM):
        self.url = url
        self.cap = open_stream(url)
        if not self.cap.isOpened():
            raise ConnectionError(f"Couldn't open the camera stream at {url}. "
                                  "Is the network cable plugged in, and this computer's network adapter set to 192.168.31.50?")
        self.frame = None
        self.frame_time = 0.0
        self.frames_read = 0
        self.reconnects = 0
        self.running = True
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def _read_loop(self):
        last_good = time.time()
        while self.running:
            ok, frame = self.cap.read() if self.cap is not None else (False, None)
            if ok:
                last_good = time.time()
                with self.lock:
                    self.frame, self.frame_time = frame, last_good
                    self.frames_read += 1
                continue
            time.sleep(0.05)
            if time.time() - last_good > RECONNECT_AFTER and self.running:
                # The stream has stalled: drop it and try to connect again.
                if self.cap is not None:
                    self.cap.release()
                self.cap = open_stream(self.url)
                if not self.cap.isOpened():
                    self.cap.release()
                    self.cap = None
                    time.sleep(0.5)
                else:
                    self.reconnects += 1
                last_good = time.time()          # give the new connection a chance

    def frame_age(self):
        """Seconds since the newest frame arrived (infinity if none yet)."""
        with self.lock:
            return time.time() - self.frame_time if self.frame is not None else float("inf")

    def latest_with_count(self):
        """(newest frame, how many frames have been read so far), without waiting."""
        with self.lock:
            return (None if self.frame is None else self.frame.copy()), self.frames_read

    def latest(self, timeout=5.0):
        """The newest frame (waits up to `timeout` seconds for the first one)."""
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if self.frame is not None:
                    return self.frame.copy()
            time.sleep(0.01)
        raise TimeoutError("No frames received from the camera.")

    def close(self):
        self.running = False
        self.thread.join(timeout=OPEN_TIMEOUT_MS / 1000 + READ_TIMEOUT_MS / 1000 + 1)
        if self.cap is not None:
            self.cap.release()


if __name__ == "__main__":
    import numpy as np
    import counter

    url = SUB_STREAM if "sub" in sys.argv[1:] else MAIN_STREAM
    cam = LiveCamera(url)
    try:
        frame = cam.latest()
        time.sleep(1.0)                      # let the stream settle
        frame = cam.latest()
    finally:
        cam.close()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs("snapshots", exist_ok=True)
    path = f"snapshots/live_{stamp}.png"
    cv2.imwrite(path, frame)
    print(f"Saved {path} ({frame.shape[1]} x {frame.shape[0]})")

    result = counter.count_image(frame)
    s = result.spots
    inside = counter.in_block(s[:, counter.X], s[:, counter.Y], s[:, counter.R], result.block)
    inside &= counter.big_enough(s[:, counter.R])
    if result.block is None:
        print(f"No 4x4 counting block found ({len(s)} cell-like spots in view). Is the grid in focus?")
    else:
        pale = np.array([lab == counter.PALE for lab in result.labels], dtype=bool)
        print(f"{result.corner} corner: {int(inside.sum())} cells ({int((inside & pale).sum())} pale)")
    print("Times (ms):", {k: round(v * 1000) for k, v in result.timings.items()})
