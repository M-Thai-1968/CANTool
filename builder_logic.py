"""Logik des GUI Builders (ohne Qt): Elemente, Auto-Skala, Zeigerwinkel, Textformat."""
from __future__ import annotations

import math
import uuid
from typing import Any

KINDS = ("background", "image", "led", "text", "gauge")
FUNCTION_KINDS = ("led", "text", "gauge")
DEFAULT_SIZE = {"background": (300, 200), "image": (200, 150), "led": (110, 56), "text": (190, 40), "gauge": (240, 150)}
LED_COLORS = {"on": "#2ecc40", "off": "#555555"}
BG_COLOR = "#cfd8dc"
NO_VALUE_COLOR = "#555555"
LED_MODES = ("digital", "range")
# Standardfarben: kleiner / gleich / groesser als x
RANGE_COLORS = {"lt": "#3498db", "eq": "#2ecc40", "gt": "#e74c3c"}


def new_element(kind: str, x: int = 20, y: int = 20) -> dict[str, Any]:
    """Neues Element mit Standardwerten (alle Werte JSON-tauglich, damit sie im Projekt gespeichert werden)."""
    if kind not in KINDS:
        raise ValueError(f"unbekanntes Element: {kind}")
    w, h = DEFAULT_SIZE[kind]
    el: dict[str, Any] = {"id": uuid.uuid4().hex[:8], "kind": kind, "x": int(x), "y": int(y), "w": w, "h": h,
                          "signal": "", "text": ""}
    if kind == "background":
        el.update(color=BG_COLOR)
    elif kind == "image":
        el.update(path="")
    elif kind == "led":
        el.update(mode="digital", c0=LED_COLORS["off"], c1=LED_COLORS["on"], ranges=default_ranges())
    elif kind == "text":
        el.update(decimals=2)
    else:
        el.update(auto=True, min=0.0, max=100.0, decimals=1, title_on=True, range_on=False,
                  ranges=default_ranges(), show_min=False, show_max=False)
    return el


def led_state(value: float | None, threshold: float = 0.0) -> bool:
    """LED leuchtet, wenn ein Wert vorliegt und groesser als die Schwelle ist."""
    return value is not None and value > threshold


def compare_class(value: float | None, x: float) -> str | None:
    """'lt', 'eq' oder 'gt' (Wert kleiner / gleich / groesser als x); None ohne Wert."""
    if value is None or isinstance(value, float) and math.isnan(value):
        return None
    if math.isclose(value, x, rel_tol=1e-9, abs_tol=1e-9):
        return "eq"
    return "lt" if value < x else "gt"


def led_color(el: dict[str, Any], value: float | None) -> str:
    """Farbe der LED. Digital: Wert 0 -> c0, sonst c1. Bereiche: erster aktiver Bereich mit min <= x < max."""
    if value is None or isinstance(value, float) and math.isnan(value):
        return NO_VALUE_COLOR
    if el.get("mode", "digital") == "range":
        for r in el.get("ranges", [])[:RANGE_COUNT]:
            if r.get("on", True) and in_range(value, float(r["min"]), float(r["max"])):
                return str(r.get("color", NO_VALUE_COLOR))
        return NO_VALUE_COLOR
    return str(el.get("c1" if value != 0 else "c0", NO_VALUE_COLOR))


RANGE_COUNT = 3


def default_ranges() -> list[dict[str, Any]]:
    """Drei Bereiche der Skala: min <= x < max mit je einer Farbe."""
    return [{"on": True, "min": 0.0, "max": 50.0, "color": "#2ecc40"},
            {"on": True, "min": 50.0, "max": 80.0, "color": "#f1c40f"},
            {"on": True, "min": 80.0, "max": 100.0, "color": "#e74c3c"}]


def in_range(value: float, lo: float, hi: float) -> bool:
    """Bereichsregel: min <= x < max (Minimum gehoert dazu, Maximum nicht)."""
    return lo <= value < hi


def gauge_bands(lo: float, hi: float, ranges: list[dict[str, Any]]) -> list[tuple[float, float, str]]:
    """Bereichsanzeige: [(von, bis, Farbe)] als Skalenanteile 0..1; auf die Skala begrenzt, leere/inaktive entfallen."""
    out: list[tuple[float, float, str]] = []
    if hi <= lo:
        return out
    for r in ranges[:RANGE_COUNT]:
        if not r.get("on", True):
            continue
        a, b = max(float(r["min"]), lo), min(float(r["max"]), hi)
        if b > a:
            out.append(((a - lo) / (hi - lo), (b - lo) / (hi - lo), str(r.get("color", "#888888"))))
    return out


def _compare_ranges(x: float, clt: str, ceq: str, cgt: str) -> list[dict[str, Any]]:
    """Alte Vergleichsfarben (kleiner / gleich / groesser x) als drei Bereiche min <= x < max."""
    up = math.nextafter(x, math.inf)
    return [{"on": True, "min": -1e12, "max": x, "color": clt}, {"on": True, "min": x, "max": up, "color": ceq},
            {"on": True, "min": up, "max": 1e12, "color": cgt}]


def sanitize_ranges(value: Any) -> list[dict[str, Any]]:
    """Bereichsliste aus dem Projekt pruefen; ungueltige Eintraege (min >= max, falsche Typen) fallen auf Standard zurueck."""
    base = default_ranges()
    if not isinstance(value, list):
        return base
    for i, r in enumerate(value[:RANGE_COUNT]):
        try:
            lo, hi = manual_range(r["min"], r["max"])
            base[i] = {"on": bool(r.get("on", True)), "min": lo, "max": hi, "color": str(r.get("color", base[i]["color"]))}
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    return base


def format_value(value: float | None, decimals: int = 2, unit: str = "") -> str:
    if value is None or isinstance(value, float) and math.isnan(value):
        return "--"
    text = f"{value:.{max(0, int(decimals))}f}"
    return f"{text} {unit}".strip()


def nice_number(x: float, up: bool = True) -> float:
    """Rundet auf 1, 2, 2.5, 5, 10 * 10^n (auf- bzw. abwaerts)."""
    if x == 0:
        return 0.0
    sign = -1 if x < 0 else 1
    ax = abs(x)
    exp = math.floor(math.log10(ax))
    base = 10 ** exp
    frac = ax / base
    steps = (1, 2, 2.5, 5, 10)
    if up:                                     # betragsmaessig aufrunden (weg von 0)
        pick = next((s for s in steps if s >= frac - 1e-12), 10)
    else:                                      # betragsmaessig abrunden
        pick = max((s for s in steps if s <= frac + 1e-12), default=1)
    return sign * pick * base


def nice_range(lo: float, hi: float) -> tuple[float, float]:
    """Gerundeter Skalenbereich um [lo, hi]; enthaelt immer 0, wenn alle Werte gleiches Vorzeichen haben und nah an 0."""
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        return (0.0, 1.0) if hi == 0 else ((0.0, nice_number(hi)) if hi > 0 else (nice_number(hi), 0.0))
    span = hi - lo
    if lo >= 0 and lo < 0.4 * hi:
        lo = 0.0
    elif hi <= 0 and hi > 0.4 * lo:
        hi = 0.0
    step = nice_number(span / 5)
    return math.floor(lo / step + 1e-9) * step, math.ceil(hi / step - 1e-9) * step


class MinMax:
    """Kleinster und groesster beobachteter Wert (fuer Min-/Max-Zeiger)."""

    def __init__(self) -> None:
        self.lo: float | None = None
        self.hi: float | None = None

    def reset(self) -> None:
        self.lo = self.hi = None

    def update(self, value: float | None) -> None:
        if value is None or isinstance(value, float) and math.isnan(value):
            return
        self.lo = value if self.lo is None else min(self.lo, value)
        self.hi = value if self.hi is None else max(self.hi, value)


class AutoScale:
    """Automatische Skala: waechst mit den beobachteten Werten, Grenzen werden auf runde Zahlen gesetzt."""

    def __init__(self) -> None:
        self.lo: float | None = None
        self.hi: float | None = None

    def reset(self) -> None:
        self.lo = self.hi = None

    def update(self, value: float | None) -> None:
        if value is None or isinstance(value, float) and math.isnan(value):
            return
        self.lo = value if self.lo is None else min(self.lo, value)
        self.hi = value if self.hi is None else max(self.hi, value)

    def range(self) -> tuple[float, float]:
        if self.lo is None or self.hi is None:
            return 0.0, 100.0
        return nice_range(self.lo, self.hi)


def gauge_fraction(value: float | None, lo: float, hi: float) -> float:
    """Zeigerstellung 0..1 (0 = linker Anschlag, 1 = rechter Anschlag, begrenzt)."""
    if value is None or hi == lo:
        return 0.0
    return min(1.0, max(0.0, (value - lo) / (hi - lo)))


def gauge_angle(fraction: float) -> float:
    """Winkel in Grad im mathematischen Sinn: 180 = links, 90 = oben, 0 = rechts (Halbkreis-Skala)."""
    return 180.0 - 180.0 * min(1.0, max(0.0, fraction))


def tick_values(lo: float, hi: float, count: int = 5) -> list[float]:
    count = max(2, int(count))
    return [lo + (hi - lo) * i / (count - 1) for i in range(count)]


def manual_range(lo: Any, hi: Any) -> tuple[float, float]:
    """Pruefung der manuellen Skalenvorgabe (min muss kleiner als max sein)."""
    lo, hi = float(lo), float(hi)
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo >= hi:
        raise ValueError("min < max erforderlich")
    return lo, hi


def sanitize(elements: Any) -> list[dict[str, Any]]:
    """Aus dem Projekt geladene Elemente pruefen und mit Standardwerten ergaenzen; Unbrauchbares entfaellt."""
    out = []
    if not isinstance(elements, list):
        return out
    for e in elements:
        if not isinstance(e, dict) or e.get("kind") not in KINDS:
            continue
        base = new_element(e["kind"])
        e = dict(e)
        if e["kind"] == "led" and "mode" not in e and ("threshold" in e or "color" in e):   # alte Projekte
            thr = float(e.get("threshold", 0.0) or 0.0)
            e["c1"] = e.get("color", LED_COLORS["on"])
            if thr != 0:
                e.update(mode="range", ranges=_compare_ranges(thr, LED_COLORS["off"], LED_COLORS["off"], e["c1"]))
        base.update({k: v for k, v in e.items() if k in base})
        if e.get("mode") == "analog":                                           # Zwischenstand: kleiner/gleich/groesser x
            xv = float(e.get("xv", 0.0))
            e.update(mode="range", ranges=_compare_ranges(xv, e.get("clt", "#3498db"), e.get("ceq", "#2ecc40"),
                                                          e.get("cgt", "#e74c3c")))
        base.update({k: v for k, v in e.items() if k in base})
        if "ranges" in base:
            base["ranges"] = sanitize_ranges(e.get("ranges"))
        for k in ("x", "y", "w", "h"):
            base[k] = max(0 if k in "xy" else 30, int(base[k]))
        out.append(base)
    return out
