"""Edge-only GPIO callbacks; deterministic nonblocking debounce and semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

KEYS = ("up", "down", "left", "right", "back", "ok")


@dataclass(frozen=True)
class KeyEvent:
    key: str
    kind: str = "short"
    generation: int = 0
    press_id: int = 0


class InputInterpreter:
    def __init__(self, config: dict):
        self.c = config
        self.raw = dict.fromkeys(KEYS, False)
        self.stable = dict.fromkeys(KEYS, False)
        self.changed = dict.fromkeys(KEYS, 0.0)
        self.pressed: dict[str, float] = {}
        self.next_repeat: dict[str, float] = {}
        self.generations: dict[str, int] = {}
        self.ids: dict[str, int] = {}
        self.long_sent: set[str] = set()
        self.immediate: set[str] = set()
        self.enabled = False
        self.release_since: float | None = None
        self.chord = False
        self.serial = 0

    def edge(self, key: str, pressed: bool, now: float) -> None:
        if key in self.raw and self.raw[key] != pressed:
            self.raw[key] = pressed
            self.changed[key] = now

    def tick(self, now: float, generation: int, immediate_back: bool = False) -> list[KeyEvent]:
        result = []
        for key in KEYS:
            if now - self.changed[key] >= self.c["debounce_ms"] / 1000:
                old, new = self.stable[key], self.raw[key]
                self.stable[key] = new
                if old != new:
                    if new:
                        self.serial += 1
                        self.ids[key] = self.serial
                        self.pressed[key] = now
                        self.generations[key] = generation
                        self.next_repeat[key] = now + self.c["repeat_delay_ms"] / 1000
                        if immediate_back and key == "back" and self.enabled:
                            result.append(KeyEvent(key, "short", generation, self.serial))
                            self.immediate.add(key)
                    else:
                        if (
                            self.enabled
                            and not self.chord
                            and key not in self.long_sent | self.immediate
                        ):
                            result.append(
                                KeyEvent(
                                    key,
                                    "short",
                                    self.generations.get(key, generation),
                                    self.ids.get(key, 0),
                                )
                            )
                        self.pressed.pop(key, None)
                        self.long_sent.discard(key)
                        self.immediate.discard(key)
        held = sum(self.stable.values())
        if held > 1:
            self.chord = True
            result = [e for e in result if e.key == "back" and immediate_back]
        if not held:
            self.chord = False
            if self.release_since is None:
                self.release_since = now
            if now - self.release_since >= self.c["released_before_enable_ms"] / 1000:
                self.enabled = True
        else:
            self.release_since = None
        if self.enabled and not self.chord:
            for key, started in self.pressed.items():
                gen, pid = self.generations[key], self.ids[key]
                if (
                    key == "back"
                    and key not in self.long_sent | self.immediate
                    and now - started >= self.c["long_press_ms"] / 1000
                ):
                    result.append(KeyEvent(key, "long", gen, pid))
                    self.long_sent.add(key)
                if key in ("up", "down", "left", "right") and now >= self.next_repeat[key]:
                    result.append(KeyEvent(key, "repeat", gen, pid))
                    self.next_repeat[key] = now + 1 / self.c["repeat_hz"]
        return result


class GPIOButtons:
    def __init__(self, config: dict, emit: Callable):
        import time
        from gpiozero import Button
        from gpiozero.pins.lgpio import LGPIOFactory

        import glob
        import lgpio

        chips = []
        for path in glob.glob("/dev/gpiochip*"):
            handle = lgpio.gpiochip_open(int(path.removeprefix("/dev/gpiochip")))
            try:
                label = lgpio.gpio_get_chip_info(handle)[-1]
                if label in ("pinctrl-bcm2711", "pinctrl-rp1", "pinctrl-bcm2835"):
                    chips.append(int(path.removeprefix("/dev/gpiochip")))
            finally:
                lgpio.gpiochip_close(handle)
        if len(chips) != 1:
            raise RuntimeError(f"Expected one Raspberry Pi header GPIO chip, got {chips}")
        self.factory = LGPIOFactory(chip=chips[0])
        self.buttons = []
        try:
            for key in KEYS:
                button = Button(
                    config[key], pull_up=True, bounce_time=None, pin_factory=self.factory
                )
                # Debounce is applied once, by InputInterpreter; callbacks only enqueue edges.
                button.when_pressed = lambda k=key: emit(k, True, time.monotonic())
                button.when_released = lambda k=key: emit(k, False, time.monotonic())
                self.buttons.append(button)
                emit(key, button.is_pressed, time.monotonic())
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for button in self.buttons:
            button.close()
        self.factory.close()
