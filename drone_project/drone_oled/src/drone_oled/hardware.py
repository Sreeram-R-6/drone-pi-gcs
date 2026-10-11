"""Retrying display adapter; only the main loop may call show()."""

import logging
import threading
from typing import Any
from PIL import Image

LOG = logging.getLogger(__name__)


class Display:
    def __init__(self, config: dict, factory=None):
        self.c, self.factory = config, factory
        self.device: Any = None
        self.last_frame: bytes | None = None
        self.next_try = 0.0
        self.backoff = 0.25
        self.owner = threading.get_ident()
        self.contrast = config["contrast"]
        self.sleeping = False

    def open(self):
        if self.factory:
            self.device = self.factory()
        else:
            from luma.core.interface.serial import i2c
            from luma.oled.device import ssd1306, sh1106

            serial = i2c(port=self.c["bus"], address=self.c["address"])
            constructor = ssd1306 if self.c["driver"] == "ssd1306" else sh1106
            self.device = constructor(serial, width=128, height=64, rotate=self.c["rotation"])
        self.device.contrast(self.contrast)

    def show(self, frame: Image.Image, now: float) -> bool:
        if threading.get_ident() != self.owner:
            raise RuntimeError("Only the render thread may write the OLED")
        if frame.mode != "1" or frame.size != (128, 64):
            raise ValueError("Expected 128x64 one-bit frame")
        data = frame.tobytes()
        if now < self.next_try or (self.device and data == self.last_frame):
            return False
        try:
            if self.device is None:
                self.open()
            self.device.display(frame)
            self.last_frame = data
            self.backoff = 0.25
            return True
        except (OSError, RuntimeError, ImportError):
            LOG.exception("OLED unavailable; retry in %.2fs", self.backoff)
            if self.device:
                try:
                    self.device.cleanup()
                except Exception:
                    LOG.debug("Display cleanup failed", exc_info=True)
            self.device = None
            self.last_frame = None
            self.next_try = now + self.backoff
            self.backoff = min(10, self.backoff * 2)
            return False

    def set_contrast(self, value: int):
        value = min(255, max(0, value))
        if value != self.contrast:
            self.contrast = value
            if self.device:
                try:
                    self.device.contrast(value)
                except OSError:
                    LOG.warning("Cannot set OLED contrast", exc_info=True)

    def close(self, clear: bool = True):
        if self.device:
            try:
                if clear:
                    self.device.display(Image.new("1", (128, 64)))
                self.device.cleanup()
            except OSError:
                LOG.warning("OLED shutdown clear failed", exc_info=True)
            self.device = None
