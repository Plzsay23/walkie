"""카메라 캡처와 JPEG 프레임 분배.

영상은 JPEG 프레임을 이어 보내는 방식(MJPEG)이다. 탭 브라우저는 <img> 하나로 받는다.
노드끼리는 [길이 4바이트][JPEG] 를 이어 보낸다(/cam/raw).
"""

import asyncio
import logging
import os
import threading
import time

log = logging.getLogger("walkie.camera")

PICAM_NAMES = ("picam", "picamera", "rpicam", "csi")     # --camera picam 으로 파이 카메라 사용

# 공유를 껐다 바로 켜면 이전 스레드가 장치를 놓기 전에 새 스레드가 열려고 한다. 차례를 지키게 한다.
_device_lock = threading.Lock()


class FrameHub:
    """최신 프레임 하나를 들고 있다가 기다리는 쪽들에게 알린다. 이벤트 루프 스레드에서만 만진다."""

    def __init__(self):
        self.frame = None
        self.seq = 0
        self._event = asyncio.Event()

    def publish(self, frame):
        self.frame = frame
        self.seq += 1
        self._event.set()
        self._event = asyncio.Event()

    def clear(self):
        self.frame = None

    async def next(self, last_seq, timeout):
        """last_seq 다음 프레임을 돌려준다. timeout 안에 없으면 (None, last_seq)."""
        if self.seq != last_seq and self.frame is not None:
            return self.frame, self.seq
        ev = self._event
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            return None, last_seq
        return self.frame, self.seq


class Camera:
    """별도 스레드에서 캡처하고 JPEG 로 인코딩해 hub 에 넣는다.

    공유를 켤 때만 장치를 연다. 꺼져 있으면 카메라 표시등도 꺼져 있어야 한다.
    """

    def __init__(self, source, hub, loop, size=(960, 540), fps=12, quality=70, on_fail=None):
        self.source = source
        self.hub = hub
        self.loop = loop
        self.size = size
        self.fps = fps
        self.quality = quality
        self.on_fail = on_fail
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()        # 스레드는 다음 프레임에서 스스로 장치를 닫는다. 여기서 기다리지 않는다.

    def _fail(self, why):
        log.error("카메라: %s", why)
        if self.on_fail and not self._stop.is_set():
            self.loop.call_soon_threadsafe(self.on_fail, why)

    def _run(self):
        with _device_lock:
            if self._stop.is_set():
                return
            if str(self.source) in PICAM_NAMES:
                self._capture_picam()
            else:
                self._capture()

    def _capture_picam(self):
        """파이 카메라(CSI). rpicam-vid 가 MJPEG 를 표준출력으로 흘려 주면 프레임 단위로 잘라 쓴다.

        파이 카메라는 libcamera 를 거쳐야 해서 OpenCV 로 직접 못 연다.
        인코딩도 이 도구가 하므로 파이 CPU 부담이 적다.
        """
        import shutil
        import subprocess

        exe = next((e for e in ("rpicam-vid", "libcamera-vid") if shutil.which(e)), None)
        if exe is None:
            return self._fail("rpicam-vid 가 없습니다 (Raspberry Pi OS 가 아닌 듯합니다)")
        cmd = [exe, "--codec", "mjpeg", "--timeout", "0", "--nopreview",
               "--width", str(self.size[0]), "--height", str(self.size[1]),
               "--framerate", str(self.fps), "--quality", str(self.quality), "--output", "-"]
        log.info("카메라 켬 (%s, %dx%d, %dfps)", exe, *self.size, self.fps)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        buf = b""
        try:
            while not self._stop.is_set():
                chunk = proc.stdout.read(65536)
                if not chunk:
                    if self._stop.is_set():
                        return
                    return self._fail("영상이 끊겼습니다")
                buf += chunk
                while True:                      # JPEG 는 FFD8 로 시작해 FFD9 로 끝난다
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        buf = b""
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        buf = buf[start:]
                        break
                    self.loop.call_soon_threadsafe(self.hub.publish, buf[start:end + 2])
                    buf = buf[end + 2:]
                if len(buf) > 8_000_000:         # 어긋났을 때 무한히 쌓이지 않게
                    buf = b""
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.info("카메라 끔")

    def _capture(self):
        import cv2
        idx = int(self.source) if str(self.source).isdigit() else self.source
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(idx, backend)
        if not cap.isOpened():
            return self._fail(f"장치 {self.source} 를 열 수 없습니다")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
        log.info("카메라 켬 (%s, %dx%d, %dfps)", self.source, *self.size, self.fps)
        interval = 1.0 / self.fps
        misses = 0
        next_at = time.monotonic()
        try:
            while not self._stop.is_set():
                ok, img = cap.read()
                if not ok:
                    misses += 1
                    if misses > 30:
                        return self._fail("프레임이 들어오지 않습니다")
                    time.sleep(0.05)
                    continue
                misses = 0
                now = time.monotonic()
                if now < next_at:
                    continue                # 장치가 더 빨리 주면 버린다(버퍼가 쌓여 늦어지지 않게 계속 읽는다)
                next_at = max(next_at + interval, now)
                ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                if ok:
                    self.loop.call_soon_threadsafe(self.hub.publish, jpg.tobytes())
        finally:
            cap.release()
            log.info("카메라 끔")
