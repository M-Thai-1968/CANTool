"""make_screenshots.py - erzeugt die kommentierten Abbildungen der Hilfe (help_images/<rubrik>_<sprache>.png).

    python tools/make_screenshots.py            alle Sprachen
    python tools/make_screenshots.py de en      nur diese Sprachen

Die Abbildungen sind schematische Darstellungen der Oberflaeche (HTML-Nachbau, gerendert mit Chromium/Playwright)
und verwenden die Texte der jeweiligen Sprachdatei. Rote Nummern verweisen auf die Erklaerungen im Hilfekapitel
(Schluessel help.shot.<rubrik>.<nummer>). Nur zur Entwicklung noetig: die fertigen PNG liegen im Projekt.
Benoetigt: playwright (+ Chromium).
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from i18n import Translator  # noqa: E402

OUT = ROOT / "help_images"
TABS = ["project", "data", "trace", "sequence", "graph", "logging", "diag", "help"]
SHOTS = TABS[:7] + ["header", "help"]            # Reihenfolge der Abbildungen (header = Rubrikzeile)
SHOTS = ["project", "data", "trace", "trace_custom", "trace_custom_edit", "sequence", "graph", "graph_multi", "builder", "builder_signal", "builder_scale", "builder_range", "logging", "diag", "vflash", "header", "help"]
FONTS = '"Noto Sans","Noto Sans CJK SC","Noto Sans CJK TC","Loma","DejaVu Sans",sans-serif'

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#fff;font:13px %s;color:#202020}
.win{width:1100px;border:1px solid #9a9a9a;background:#efefef}
.title{background:#dcdcdc;padding:5px 10px;font-size:12px;border-bottom:1px solid #b5b5b5}
.tabs{display:flex;align-items:flex-end;padding:6px 6px 0;background:#efefef;border-bottom:1px solid #b5b5b5}
.tab{padding:5px 14px;border:1px solid #b5b5b5;border-bottom:none;background:#e2e2e2;margin-right:2px;border-radius:3px 3px 0 0}
.tab.on{background:#fff;font-weight:600;position:relative;top:1px}
.corner{margin-left:auto;display:flex;align-items:center;gap:8px;padding:0 8px 5px}
.page{background:#fff;padding:10px 12px;min-height:350px;position:relative}
.row{display:flex;align-items:center;gap:8px;margin:5px 0;flex-wrap:wrap}
.btn{border:1px solid #a0a0a0;background:linear-gradient(#fafafa,#e6e6e6);border-radius:3px;padding:3px 12px;white-space:nowrap}
.btn.red{background:#f4d0d0}.btn.green{background:#d4efd4}
.in{border:1px solid #a0a0a0;background:#fff;padding:2px 6px;min-width:120px;height:23px;display:inline-flex;align-items:center}
.in.sm{min-width:60px}.in.lg{min-width:260px}
.dd:after{content:"\\25BE";margin-left:auto;padding-left:10px;color:#555}
.chk:before{content:"";display:inline-block;width:12px;height:12px;border:1px solid #777;background:#fff;margin-right:6px;vertical-align:-2px}
.chk{margin-right:12px}
.chk.on:before{background:#2a6ebe;box-shadow:inset 0 0 0 2px #fff}
.grp{border:1px solid #bdbdbd;border-radius:3px;padding:10px 8px 6px;margin:12px 0 6px;position:relative}
.grp>.gt{position:absolute;top:-9px;left:8px;background:#fff;padding:0 4px;color:#444}
table.t{border-collapse:collapse;width:100%%;background:#fff}
table.t th{background:#e8e8e8;border:1px solid #c4c4c4;padding:3px 6px;text-align:left;font-weight:600;white-space:nowrap}
table.t td{border:1px solid #dedede;padding:2px 6px;white-space:nowrap}
table.t tr.sel td{background:#cfe3f8}.ok td{background:#d7f5d7}.bad td{background:#f8d0d0}
.mono{font-family:"DejaVu Sans Mono",monospace;font-size:12px}
.cols{display:flex;gap:12px}.cols>div{flex:1;min-width:0}
.status{color:#444;margin:4px 0}
.tip{position:absolute;background:#fffbe6;border:1px solid #888;padding:6px 8px;font-size:12px;width:430px;box-shadow:2px 2px 6px rgba(0,0,0,.25)}
.tip pre{margin:3px 0;background:#f4f4f4;padding:3px;font-size:11px;white-space:pre-wrap}
.b{position:absolute;width:22px;height:22px;border-radius:50%%;background:#d32f2f;color:#fff;font:700 13px sans-serif;
   display:flex;align-items:center;justify-content:center;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.5);z-index:9;font-style:normal}
.list{border:1px solid #a0a0a0;background:#fff;padding:2px}.list div{padding:2px 6px}.list .sel{background:#2a6ebe;color:#fff}
.plot{border:1px solid #a0a0a0;background:#fff;height:300px}
""" % FONTS

JS = """
document.querySelectorAll('[data-n]').forEach(e=>{
  const r=e.getBoundingClientRect(), p=document.querySelector('.win').getBoundingClientRect();
  const b=document.createElement('i'); b.className='b'; b.textContent=e.dataset.n;
  b.style.left=Math.max(2, r.left-p.left-11)+'px'; b.style.top=Math.max(2, r.top-p.top-11)+'px';
  document.querySelector('.win').appendChild(b);
});
"""


class Ctx:
    def __init__(self, texts: dict[str, str]) -> None:
        self.t = texts

    def __call__(self, key: str, **kw) -> str:
        text = self.t.get(key, key)
        return html.escape(text.format(**kw) if kw else text)


def n(num: int | None, inner: str) -> str:
    """Element mit Erklaerungs-Nummer markieren (ohne Layout-Wirkung)."""
    return inner if num is None else f'<span data-n="{num}" style="display:inline-flex">{inner}</span>'


def btn(c: Ctx, key: str, cls: str = "") -> str:
    return f'<span class="btn {cls}">{c(key)}</span>'


def inp(text: str = "", cls: str = "", dd: bool = False) -> str:
    return f'<span class="in {cls} {"dd" if dd else ""}">{html.escape(text)}</span>'


def chk(c: Ctx, key: str, on: bool = False) -> str:
    return f'<span class="chk {"on" if on else ""}">{c(key)}</span>'


def table(heads: list[str], rows: list[list[str]], cls: str = "", sel: set[int] = frozenset(),
          rowcls: dict[int, str] | None = None, rowcolor: list[str] | None = None) -> str:
    head = "".join(f"<th>{h}</th>" for h in heads)
    body = ""
    for i, r in enumerate(rows):
        klass = ("sel " if i in sel else "") + (rowcls or {}).get(i, "")
        style = f' style="color:{rowcolor[i]}"' if rowcolor else ""
        body += f'<tr class="{klass}"{style}>' + "".join(f"<td>{x}</td>" for x in r) + "</tr>"
    return f'<table class="t {cls}"><tr>{head}</tr>{body}</table>'


# --------------------------------------------------------------------------- Rubriken
def page_project(c: Ctx) -> str:
    return f"""
<div class="row">{n(1, f'<span class="row" style="margin:0">{c("project.active")} {inp("demo", "", True)} {btn(c, "project.new")} {btn(c, "project.import")}</span>')}
 {n(2, f'<span class="row" style="margin:0">{c("project.cache_format")} {inp("json", "sm", True)} {btn(c, "project.reconvert")}</span>')}</div>
<div class="grp"><span class="gt">{c("hw.group")}</span>
 <div class="row">{n(3, f'<span class="row" style="margin:0">{c("hw.interface")} {inp(c.t["hw.type.pcan"], "lg", True)} {c("hw.channel")} {inp("PCAN_USBBUS1", "")} {c("hw.bitrate")} {inp("500", "sm")} {chk(c, "hw.fd")}</span>')}</div>
 <div class="row">{chk(c, "hw.sim_echo", True)} {chk(c, "hw.sim_ecu", True)}</div></div>
<div class="row">{n(4, f'<span class="row" style="margin:0">{btn(c, "conn.disconnect", "red")} <span class="status">{c("conn.connected", channel="PCAN_USBBUS1", rx=1284, tx=0)}</span></span>')}</div>
<div class="row" style="margin-top:12px">{c("project.info_label")}</div>
{n(5, '<div class="in mono" style="display:block;height:auto;padding:6px;line-height:1.5;width:100%">'
   + html.escape(c.t["project.info.dbc"].format(sources="vehicle.dbc, engine.dbc", count=7)) + "<br>"
   + html.escape(c.t["project.info.warning"].format(text="...")) + "</div>")}"""


def page_data(c: Ctx) -> str:
    left = f"""<div>{c("data.available")}
 <div class="row">{n(1, inp("Engine", "lg"))}</div>
 {table([c("col.message_signal"), c("col.id"), c("col.unit")],
        [["EngineData", "100", ""], ["&nbsp;&nbsp;RPM", "", "rpm"], ["&nbsp;&nbsp;CoolantTemp", "", "&deg;C"],
         ["DriverCmd", "200", ""], ["&nbsp;&nbsp;ThrottleCmd", "", "%"], ["VehicleSpeed", "300", ""]], sel={1})}
 <div class="row">{n(2, btn(c, "data.add_tx") + " " + btn(c, "data.add_rx"))}</div></div>"""
    right = f"""<div><div class="grp"><span class="gt">{c("data.tx_group")}</span>
 {n(3, table([c("col.tx_message_signal"), c("col.cyclic"), c("col.value"), c("col.cycle_ms"), c("col.select")],
        [["DriverCmd", '<span class="chk on"></span>', "", "20", n(4, btn(c, "data.send_button"))],
         ["&nbsp;&nbsp;ThrottleCmd", "", "35 %", "", ""]]))}</div>
 <div class="grp"><span class="gt">{c("data.rx_group")}</span>
 {n(5, table([c("col.rx_message_signal"), c("col.value"), c("col.unit"), c("col.text")],
        [["EngineData.RPM", "2150", "rpm", ""], ["EngineData.CoolantTemp", "88", "&deg;C", ""],
         ["VehicleSpeed.Gear", "3", "", "D"]]))}</div></div>"""
    return f'<div class="cols">{left}{right}</div>'


def page_trace(c: Ctx) -> str:
    rows = [
        ["0.0000", "Rx", "PCAN_USBBUS1", "100", "CAN", "8", "00 01 02 03 04 05 06 07", "", ""],
        ["0.0100", "Rx", "PCAN_USBBUS1", "100", "CAN", "8", "0A 1F 40 00 58 00 00 00", "EngineData", "RPM=2150 rpm, CoolantTemp=88 &deg;C"],
        ["0.0200", "Tx", "PCAN_USBBUS1", "200", "CAN", "4", "23 00 01 03", "DriverCmd", "ThrottleCmd=35 %"],
        ["0.0500", "Rx", "PCAN_USBBUS1", "000", "ERR", "8", "00 00 00 00 00 00 00 00", "", c("trace.error_frame")],
        ["0.1000", "Tx", "PCAN_USBBUS1", "7E0", "CAN", "8", "03 22 F1 90 AA AA AA AA", "", ""],
        ["0.1100", "Rx", "PCAN_USBBUS1", "7E8", "CAN", "8", "03 62 F1 90 00 00 00 00", "", ""],
        ["1.0000", "Rx", "PCAN_USBBUS1", "100", "CAN", "8", "00 01 02 03 04 05 06 07", "", ""]]
    colors = ["#2f6fd0", "#2f6fd0", "#2e9e48", "#d32f2f", "#8e44ad", "#8e44ad", "#2f6fd0"]
    heads = [c(k) for k in ("trace.col.time", "trace.col.dir", "trace.col.channel", "trace.col.id", "trace.col.type",
                            "trace.col.len", "trace.col.data", "trace.col.message", "trace.col.signals")]
    t = table(heads, rows, "mono", rowcolor=colors)
    return f"""
<div class="row">{n(1, chk(c, "trace.pause") + " " + chk(c, "trace.decode", True) + " " + chk(c, "trace.only_selected") + " " + chk(c, "trace.autoscroll", True))}
 {chk(c, "trace.rx", True)} {chk(c, "trace.tx", True)} {n(2, inp("", "lg"))} {n(6, btn(c, "trace.color_mode", "green"))} {n(5, btn(c, "trace.clear") + " " + btn(c, "trace.save_csv"))}</div>
<div class="row">{n(7, c("trace.raw.id") + " " + inp("100", "") + " " + c("trace.raw.dlc") + " " + inp("8", "") + " " + c("trace.raw.data") + " " + inp("00 01 02 03 04 05 06 07", "lg") + " " + c("trace.raw.timing") + " " + inp("1000 ms", "") + " " + chk(c, "trace.raw.cyclic", True) + " " + btn(c, "trace.raw.send", "green"))}
 {btn(c, "trace.custom.button")}</div>
{n(3, t)}
<div class="status">{c("trace.count", rows=7, queued=0)}</div>
<div style="height:200px"></div>
<div data-n="4" style="position:absolute;left:640px;top:75px;width:380px;height:130px"></div>"""


def page_sequence(c: Ctx) -> str:
    steps = [["1", "set_signal", '{"message": "DriverCmd", "signal": "ThrottleCmd", "value": 40}'],
             ["2", "enable_tx", '{"message": "DriverCmd", "enabled": true}'],
             ["3", "wait_for", '{"direction": "RX", "message": "EngineData", "signal": "RPM", "op": ">", "value": 100, "timeout_ms": 3000}'],
             ["4", "diag", '{"service": "ReadVIN", "expect": "62 F1 90"}']]
    res = [["1", "DriverCmd.ThrottleCmd = 40", c("seq.status.ok"), ""],
           ["2", "DriverCmd: ON", c("seq.status.ok"), ""],
           ["3", "RX EngineData.RPM > 100", c("seq.status.fail"), "Timeout"]]
    return f"""
<div class="row">{n(1, c("seq.file") + " " + inp("startup.json", "", True) + " " + btn(c, "seq.load") + " " + btn(c, "seq.new") + " " + btn(c, "seq.save") + " " + btn(c, "seq.save_as"))}</div>
<div class="row">{c("seq.name")} {inp("startup", "")} {c("seq.description")} {inp("", "lg")}</div>
<div class="row">{n(2, inp("wait_for  -  " + c.t["steps.wait_for.title"], "lg", True))} {n(3, btn(c, "seq.add_step") + " " + btn(c, "seq.up") + " " + btn(c, "seq.down") + " " + btn(c, "seq.duplicate") + " " + btn(c, "seq.delete"))}</div>
{n(4, table([c("seq.col.num"), c("seq.col.type"), c("seq.col.params")], steps, "mono", sel={2}))}
<div style="height:8px"></div>
{n(6, table([c("seq.res.step"), c("seq.res.description"), c("seq.res.status"), c("seq.res.message")], res, "", rowcls={0: "ok", 1: "ok", 2: "bad"}))}
<div class="row" style="margin-top:8px">{n(5, btn(c, "seq.run") + " " + btn(c, "seq.stop") + " " + btn(c, "seq.check"))}
 <span class="status">{c("seq.summary", name="startup", status=c.t["seq.run_status.failed"], ok=2, total=3)}</span></div>
<div class="tip" style="left:470px;top:265px"><b>wait_for</b> &ndash; {c("steps.wait_for.title")}<br>{c("steps.wait_for.summary")}
 <div><b>{c("steps.label.format")}</b></div><pre>{html.escape(c.t["steps.wait_for.format"])}</pre></div>"""


def _graph_tree(c: Ctx, multi: bool = False) -> str:
    def row(txt, cls="", pad=0):
        return f'<div style="padding-left:{pad}px"><span class="chk {cls}">{txt}</span></div>'
    return ('<div class="list" style="height:210px">'
            + row("RX: EngineData", "on") + row("RPM [rpm]", "on", 28) + row("CoolantTemp [degC]", "on", 28)
            + row("TX: DriverCmd", "", 0) + row("ThrottleCmd [%]", "", 28)
            + row("RX: VehicleSpeed", "on" if multi else "", 0) + row("Speed [km/h]", "on" if multi else "", 28) + "</div>")


def _grid(w, h, nx=10, ny=4):
    g = "".join(f'<line x1="{x}" y1="0" x2="{x}" y2="{h}" stroke="#e5e5e5"/>' for x in range(0, w, w // nx))
    return g + "".join(f'<line x1="0" y1="{y}" x2="{w}" y2="{y}" stroke="#e5e5e5"/>' for y in range(0, h, h // ny))


def page_graph(c: Ctx) -> str:
    curves = {
        "#1f77b4": "M0,200 C60,120 120,60 180,90 S300,230 360,170 S480,40 560,90 S680,200 760,120",
        "#d62728": "M0,230 C80,230 140,200 200,180 S320,140 400,150 S560,200 640,170 S720,120 760,110",
    }
    paths = "".join(f'<path d="{d}" stroke="{col}" stroke-width="2" fill="none"/>' for col, d in curves.items())
    return f"""<div class="cols"><div style="flex:0 0 270px">{c("graph.signals")}
 {n(1, f'<div class="row" style="margin:2px 0">{btn(c, "graph.mode_single")}</div>')}
 {n(2, _graph_tree(c))}
 <div class="row" style="margin-top:10px">{n(3, f'<span class="row" style="margin:0">{c("graph.window")} {inp("30 s", "sm")}</span>')}</div>
 <div class="row">{n(4, chk(c, "graph.pause") + " " + btn(c, "graph.clear"))}</div></div>
 <div style="position:relative">{n(5, f'<svg class="plot" width="790" height="300">{_grid(780, 300)}{paths}</svg>')}
 <div style="position:absolute;left:560px;top:12px;background:#fff;border:1px solid #bbb;padding:4px 8px;font-size:12px">
  <div><span style="color:#1f77b4">&#9644;</span> EngineData.RPM</div><div><span style="color:#d62728">&#9644;</span> EngineData.CoolantTemp</div></div>
 <div style="text-align:center;color:#444;margin-top:4px">{c("graph.time")} (s)</div></div></div>"""


def page_graph_multi(c: Ctx) -> str:
    plots = [("#1f77b4", "EngineData.RPM [rpm]", "M0,70 C60,30 120,10 180,25 S300,75 360,55 S480,10 560,30 S680,65 760,40"),
             ("#d62728", "EngineData.CoolantTemp [degC]", "M0,75 C80,75 140,65 200,58 S320,45 400,48 S560,65 640,55 S720,40 760,36"),
             ("#2ca02c", "VehicleSpeed.Speed [km/h]", "M0,60 C100,60 160,30 260,30 S420,70 520,60 S680,20 760,22")]
    out = ""
    for i, (col, label, d) in enumerate(plots):
        last = i == len(plots) - 1
        h = 100
        svg = (f'<svg class="plot" width="790" height="{h}" style="height:{h}px">{_grid(780, h, 10, 2)}'
               f'<path d="{d}" stroke="{col}" stroke-width="2" fill="none"/>'
               f'<text x="8" y="14" font-size="11" fill="#333">{label}</text></svg>')
        out += n(3, svg) if i == 0 else svg
        if not last:
            out += '<div style="height:3px"></div>'
    return f"""<div class="cols"><div style="flex:0 0 270px">{c("graph.signals")}
 {n(1, f'<div class="row" style="margin:2px 0">{btn(c, "graph.mode_multi")}</div>')}
 {n(2, _graph_tree(c, True))}
 <div class="row" style="margin-top:10px">{c("graph.window")} {inp("30 s", "sm")}</div>
 <div class="row">{chk(c, "graph.pause")} {btn(c, "graph.clear")}</div></div>
 <div>{out}<div style="text-align:center;color:#444;margin-top:4px">{n(4, c("graph.time") + " (s)")}</div></div></div>"""


def page_trace_custom(c: Ctx) -> str:
    rows = [["100", "8", "00 01 02 03 04 05 06 07", "1000", "x", c("trace.custom.running")],
            ["7DF", "8", "02 01 0C 00 00 00 00 00", "100", "", ""],
            ["18FEF100", "8", "FF FF FF FF FF FF FF FF", "20", "x", c("trace.custom.running")]]
    tbl = table([c("trace.raw.id"), c("trace.raw.dlc"), c("trace.raw.data"), c("trace.raw.timing"),
                 c("trace.raw.cyclic"), c("trace.custom.status")], rows, sel={1})
    menu = ('<div data-n="3" style="position:absolute;left:330px;top:128px;background:#fff;border:1px solid #888;'
            'box-shadow:2px 2px 6px #0004;padding:3px 0;font-size:13px;min-width:150px">'
            + "".join(f'<div style="padding:3px 16px">{c(k)}</div>' for k in
                      ("trace.custom.add", "trace.raw.send", "trace.custom.edit", "trace.custom.delete")) + "</div>")
    return f"""<div class="row">{c("trace.raw.id")} {inp("100", "")} {c("trace.raw.dlc")} {inp("8", "")} {c("trace.raw.data")} {inp("00 01 02 03 04 05 06 07", "lg")}
 {btn(c, "trace.raw.send", "green")} {n(1, btn(c, "trace.custom.button"))}</div>
 <div style="position:relative;margin:14px 0 0 40px;width:760px;border:1px solid #888;background:#f6f6f6;box-shadow:3px 3px 10px #0005">
  <div style="background:#dde6f3;padding:5px 10px;font-weight:600">{c("trace.custom.title")}</div>
  <div style="padding:10px"><div style="color:#444;margin-bottom:8px">{c("trace.custom.tip")}</div>
   {n(2, tbl)}
   <div style="text-align:right;margin-top:8px">{btn(c, "flash.dlg.close")}</div></div>
  {menu}
 </div>"""


def _led(on: bool, label: str, x: int, y: int, num: int | None = None, color: str | None = None) -> str:
    col = color or ("#2ecc40" if on else "#555555")
    glow = f'<circle cx="30" cy="30" r="26" fill="{col}" opacity="0.25"/>' if (on or color) else ""
    svg = (f'<svg width="150" height="60">{glow}<circle cx="30" cy="30" r="16" fill="{col}" stroke="#222" stroke-width="2"/>'
           f'<circle cx="25" cy="25" r="4" fill="#fff" opacity="0.45"/>'
           f'<text x="56" y="35" font-size="14" fill="#202020">{label}</text></svg>')
    box = f'<div style="position:absolute;left:{x}px;top:{y}px;border:1px solid #b0b0b0;border-radius:6px;background:#f4f4f4"{f" data-n={chr(34)}{num}{chr(34)}" if num else ""}>{svg}</div>'
    return box


def _gauge(x: int, y: int, label: str, value: str, lo: str, hi: str, num: int | None = None,
           bands: list[tuple[float, float, str]] | None = None,
           extra: list[tuple[float, str]] | None = None) -> str:
    import math
    cx, cy, r = 130, 118, 90
    ticks = ""
    for i in range(5):
        a = math.radians(180 - 45 * i)
        x1, y1 = cx + r * math.cos(a), cy - r * math.sin(a)
        x2, y2 = cx + (r - 9) * math.cos(a), cy - (r - 9) * math.sin(a)
        tx, ty = cx + (r + 16) * math.cos(a), cy - (r + 11) * math.sin(a) + 4
        val = f"{(float(lo) + (float(hi) - float(lo)) * i / 4):g}"
        ticks += (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="#202020" stroke-width="2"/>'
                  f'<text x="{tx:.1f}" y="{ty:.1f}" font-size="11" text-anchor="middle" fill="#202020">{val}</text>')
    if bands:                                   # [(von, bis, Farbe)] als Skalenanteile
        rr = r - 5
        for f0, f1, col in bands:
            def pt(fr: float) -> str:
                ang = math.radians(180 - 180 * fr)
                return f"{cx + rr * math.cos(ang):.1f},{cy - rr * math.sin(ang):.1f}"
            ticks += f'<path d="M{pt(f0)} A{rr},{rr} 0 0 1 {pt(f1)}" fill="none" stroke="{col}" stroke-width="6"/>'
    for fr, col in (extra or []):               # Min-/Max-Zeiger
        ae = math.radians(180 - 180 * fr)
        ticks += (f'<line x1="{cx}" y1="{cy}" x2="{cx + (r - 4) * math.cos(ae):.1f}" y2="{cy - (r - 4) * math.sin(ae):.1f}" '
                  f'stroke="{col}" stroke-width="2" stroke-linecap="round"/>')
    a = math.radians(180 - 180 * 0.62)
    nx, ny = cx + (r - 12) * math.cos(a), cy - (r - 12) * math.sin(a)
    svg = (f'<svg width="260" height="160"><text x="130" y="15" font-size="13" text-anchor="middle" fill="#202020">{label}</text>'
           f'<path d="M{cx - r},{cy} A{r},{r} 0 0 1 {cx + r},{cy}" fill="none" stroke="#202020" stroke-width="2"/>{ticks}'
           f'<line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="#d62728" stroke-width="3" stroke-linecap="round"/>'
           f'<circle cx="{cx}" cy="{cy}" r="5" fill="#202020"/>'
           f'<text x="{cx}" y="{cy + 24}" font-size="14" text-anchor="middle" fill="#202020">{value}</text></svg>')
    return (f'<div style="position:absolute;left:{x}px;top:{y}px;border:1px solid #b0b0b0;border-radius:6px;background:#f4f4f4"'
            f'{f" data-n={chr(34)}{num}{chr(34)}" if num else ""}>{svg}</div>')


def _textbox(x: int, y: int, text: str, w: int = 230, num: int | None = None) -> str:
    return (f'<div style="position:absolute;left:{x}px;top:{y}px;width:{w}px;height:34px;line-height:34px;padding:0 8px;box-sizing:border-box;'
            f'border:1px solid #b0b0b0;border-radius:6px;background:#f4f4f4;color:#202020"'
            f'{f" data-n={chr(34)}{num}{chr(34)}" if num else ""}>{text}</div>')


def _bgbox(x: int, y: int, w: int, h: int, color: str, num: int | None = None) -> str:
    return (f'<div style="position:absolute;left:{x}px;top:{y}px;width:{w}px;height:{h}px;background:{color};border:1px solid #0003;'
            f'border-radius:8px"{f" data-n={chr(34)}{num}{chr(34)}" if num else ""}></div>')


def _imgbox(x: int, y: int, w: int, h: int, num: int | None = None) -> str:
    svg = (f'<svg width="{w}" height="{h}"><rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="6" fill="none" stroke="#b0b0b0"/>'
           f'<rect x="6" y="6" width="{w - 12}" height="{h - 12}" fill="#cfe8fa"/>'
           f'<circle cx="{w * 0.75:.0f}" cy="{h * 0.28:.0f}" r="{h * 0.1:.0f}" fill="#f6c945"/>'
           f'<path d="M6,{h - 6} L{w * 0.3:.0f},{h * 0.4:.0f} L{w * 0.5:.0f},{h * 0.65:.0f} L{w * 0.65:.0f},{h * 0.5:.0f} L{w - 6},{h - 6} Z" fill="#5b8c5a"/></svg>')
    return (f'<div style="position:absolute;left:{x}px;top:{y}px"{f" data-n={chr(34)}{num}{chr(34)}" if num else ""}>{svg}</div>')


GRID = ("background-image:linear-gradient(#ececec 1px,transparent 1px),linear-gradient(90deg,#ececec 1px,transparent 1px);"
        "background-size:20px 20px")


def _menu(c: Ctx, x: int, y: int, num: int, items: list[tuple[str, str]], width: int = 220) -> str:
    out = (f'<div data-n="{num}" style="position:absolute;left:{x}px;top:{y}px;background:#fff;border:1px solid #888;'
           f'box-shadow:2px 2px 6px #0004;padding:3px 0;font-size:13px;min-width:{width}px">')
    for kind, key in items:
        if kind == "sep":
            out += '<div style="border-top:1px solid #ccc;margin:3px 0"></div>'
            continue
        mark = "&#10003; " if kind == "chk" else ""
        sel = ";background:#2a6ebe;color:#fff" if kind == "sel" else ""
        out += f'<div style="padding:3px 16px{sel}">{mark}{c(key)}</div>'
    return out + "</div>"


def page_builder(c: Ctx) -> str:
    menu = _menu(c, 800, 50, 8, [("", "builder.menu.signal"), ("", "builder.menu.label"), ("", "builder.menu.decimals"),
                                   ("chk", "builder.menu.title_on"), ("sep", ""), ("chk", "builder.menu.scale_auto"),
                                   ("", "builder.menu.scale_manual"), ("sep", ""), ("chk", "builder.menu.range_on"),
                                   ("sel", "builder.menu.range1"), ("", "builder.menu.range2"), ("", "builder.menu.range3"), ("sep", ""), ("", "builder.menu.delete")], 200)
    btns = " ".join(f'<span class="btn">+ {c("builder.add_" + k)}</span>' for k in ("led", "text", "gauge", "background", "image"))
    return f"""<div class="row">{n(1, chk(c, "builder.edit", True))}
 {n(2, '<span class="row" style="margin:0">' + btns + '</span>')}</div>
 <div style="position:relative;height:370px;border:1px solid #a0a0a0;margin-top:8px;{GRID}">
  {_bgbox(10, 10, 400, 340, "#dbe7f5", 3)}
  {_led(True, "Ignition", 20, 20, 4, "#2ecc40")}{_led(True, "CoolantWarn", 20, 96, None, "#e74c3c")}
  {_textbox(200, 20, "Motorsteuerung", 190, 5)}{_textbox(200, 70, "EngineData.RPM 2000 rpm", 190)}
  {_textbox(200, 120, "Speed 87.5 km/h", 190)}
  {_gauge(20, 190, "VehicleSpeed.Speed", "87.5 km/h", "0", "140", 6, [(0, 0.6, "#2ecc40"), (0.6, 0.8, "#f1c40f"), (0.8, 1, "#e74c3c")])}
  {_imgbox(460, 20, 260, 150, 7)}
  {_gauge(430, 190, "EngineData.RPM", "2000.0 rpm", "0", "3200")}
  {menu}
 </div>"""


def page_builder_range(c: Ctx) -> str:
    menu = _menu(c, 480, 12, 3, [("", "builder.menu.signal"), ("", "builder.menu.label"), ("", "builder.menu.decimals"),
                                   ("chk", "builder.menu.title_on"), ("sep", ""), ("chk", "builder.menu.scale_auto"),
                                   ("", "builder.menu.scale_manual"), ("sep", ""), ("chk", "builder.menu.show_min"),
                                   ("chk", "builder.menu.show_max"), ("", "builder.menu.minmax_reset"), ("sep", ""),
                                   ("chk", "builder.menu.range_on")], 220)
    menu2 = _menu(c, 730, 210, 5, [("sel", "builder.menu.range1"), ("", "builder.menu.range2"), ("", "builder.menu.range3")], 220)
    leds = ('<div data-n="1" style="position:absolute;left:20px;top:20px;width:150px;height:240px">'
            + _led(True, "0 - 50", 0, 0, None, "#2ecc40") + _led(True, "50 - 80", 0, 80, None, "#f1c40f")
            + _led(True, "80 - 100", 0, 160, None, "#e74c3c") + "</div>")
    return f"""<div style="position:relative;height:350px;border:1px solid #a0a0a0;margin-top:8px;{GRID}">
  {leds}
  {_gauge(200, 20, "EngineData.CoolantTemp", "78.0 degC", "0", "120", 2, [(0, 0.5, "#2ecc40"), (0.5, 0.75, "#f1c40f"), (0.75, 1, "#e74c3c")], [(0.42, "#1f77b4"), (0.9, "#ff7f0e")])}
  {menu}{menu2}
  <div data-n="4" style="position:absolute;left:480px;top:274px;width:232px;height:26px"></div>
  <div data-n="6" style="position:absolute;left:480px;top:192px;width:232px;height:26px"></div>
 </div>"""


def page_trace_custom_edit(c: Ctx) -> str:
    rows = [["100", "8", "00 01 02 03 04 05 06 07", "1000", "x", c("trace.custom.running")],
            ["7DF", "8", "02 01 0C 00 00 00 00 00", "100", "", ""]]
    tbl = table([c("trace.raw.id"), c("trace.raw.dlc"), c("trace.raw.data"), c("trace.raw.timing"),
                 c("trace.raw.cyclic"), c("trace.custom.status")], rows)
    def field(label, inner):
        return f'<div class="row" style="margin:6px 0"><span style="display:inline-block;width:120px">{label}</span>{inner}</div>'
    dlg = (f'<div style="position:absolute;left:230px;top:70px;width:470px;border:1px solid #666;background:#f6f6f6;box-shadow:3px 3px 12px #0006">'
           f'<div style="background:#dde6f3;padding:5px 10px;font-weight:600">{c("trace.custom.edit_title")}</div><div style="padding:10px 16px">'
           + field(c("trace.raw.id"), n(1, inp("7DF", "")))
           + field(c("trace.raw.dlc"), n(2, inp("8", "", True)))
           + field(c("trace.raw.data"), n(3, inp("02 01 0C", "lg")))
           + field(c("trace.raw.timing"), n(4, inp("100 ms", "")) + "&nbsp; " + n(4, chk(c, "trace.raw.cyclic", True)))
           + f'<div style="text-align:right;margin-top:10px">{n(5, btn(c, "flash.status.ok"))} {btn(c, "flash.dlg.abort")}</div></div></div>')
    return f"""<div style="position:relative;height:330px">
 <div style="margin:14px 0 0 40px;width:760px;border:1px solid #888;background:#f6f6f6">
  <div style="background:#dde6f3;padding:5px 10px;font-weight:600">{c("trace.custom.title")}</div>
  <div style="padding:10px">{tbl}<div style="height:120px"></div></div></div>{dlg}</div>"""


def page_builder_signal(c: Ctx) -> str:
    items = [c("builder.no_signal"), "RX:EngineData.RPM", "RX:EngineData.CoolantTemp", "TX:DriverCmd.ThrottleCmd", "RX:VehicleSpeed.Speed"]
    lst = "".join(f'<div class="{"sel" if i == 1 else ""}">{t}</div>' for i, t in enumerate(items))
    dlg = (f'<div style="position:absolute;left:560px;top:30px;width:380px;border:1px solid #666;background:#f6f6f6;box-shadow:3px 3px 12px #0006">'
           f'<div style="background:#dde6f3;padding:5px 10px;font-weight:600">{c("builder.signal_title")}</div><div style="padding:10px 14px">'
           f'<div style="margin-bottom:6px">{c("builder.signal_prompt")}</div>{n(2, f"<div class=list>{lst}</div>")}'
           f'<div style="text-align:right;margin-top:10px">{btn(c, "flash.status.ok")} {btn(c, "flash.dlg.abort")}</div></div></div>')
    menu = ('<div data-n="1" style="position:absolute;left:150px;top:80px;background:#fff;border:1px solid #888;box-shadow:2px 2px 6px #0004;'
            'padding:3px 0;font-size:13px;min-width:190px">'
            f'<div style="padding:3px 16px;background:#2a6ebe;color:#fff">{c("builder.menu.signal")}</div>'
            + "".join(f'<div style="padding:3px 16px">{c(k)}</div>' for k in ("builder.menu.label", "builder.menu.led_digital", "builder.menu.led_analog", "builder.menu.c0", "builder.menu.c1"))
            + '<div style="border-top:1px solid #ccc;margin:3px 0"></div>'
            f'<div style="padding:3px 16px">{c("builder.menu.delete")}</div></div>')
    return f"""<div style="position:relative;height:350px;border:1px solid #a0a0a0;margin-top:8px;background-image:linear-gradient(#ececec 1px,transparent 1px),linear-gradient(90deg,#ececec 1px,transparent 1px);background-size:20px 20px">
  {_led(True, "Ignition", 20, 20, 3, "#2ecc40")}{menu}
  {_textbox(20, 290, "EngineData.RPM 2000.00 rpm", 260, 4)}
  {dlg}</div>"""


def page_builder_scale(c: Ctx) -> str:
    dlg = (f'<div style="position:absolute;left:640px;top:40px;width:330px;border:1px solid #666;background:#f6f6f6;box-shadow:3px 3px 12px #0006">'
           f'<div style="background:#dde6f3;padding:5px 10px;font-weight:600">{c("builder.scale_title")}</div><div style="padding:10px 16px">'
           f'<div class="row" style="margin:6px 0"><span style="display:inline-block;width:100px">{c("builder.min")}</span>{n(3, inp("0.000", ""))}</div>'
           f'<div class="row" style="margin:6px 0"><span style="display:inline-block;width:100px">{c("builder.max")}</span>{n(3, inp("8000.000", ""))}</div>'
           f'<div style="text-align:right;margin-top:10px">{n(4, btn(c, "flash.status.ok"))} {btn(c, "flash.dlg.abort")}</div></div></div>')
    return f"""<div style="position:relative;height:340px;border:1px solid #a0a0a0;margin-top:8px;background-image:linear-gradient(#ececec 1px,transparent 1px),linear-gradient(90deg,#ececec 1px,transparent 1px);background-size:20px 20px">
  {_gauge(20, 20, "EngineData.RPM", "2000.0 rpm", "0", "3200", 1)}
  {_gauge(320, 20, "EngineData.RPM", "2000.0 rpm", "0", "8000", 2)}
  {dlg}</div>"""


def page_logging(c: Ctx) -> str:
    tree = ('<div class="list" style="height:190px"><div>&#9660; <span class="chk on">RX: EngineData</span></div>'
            '<div style="padding-left:30px"><span class="chk on">RPM</span></div>'
            '<div style="padding-left:30px"><span class="chk on">CoolantTemp</span></div>'
            '<div>&#9660; <span class="chk">TX: DriverCmd</span></div>'
            '<div style="padding-left:30px"><span class="chk">ThrottleCmd</span></div></div>')
    return f"""<div>{n(1, f'<div>{c("log.tree_header")}{tree}</div>')}
 <div class="row">{n(2, btn(c, "log.all") + " " + btn(c, "log.none"))}</div>
 <div class="row">{n(3, f'<span class="row" style="margin:0">{c("log.filename")} {inp("demo_20261023_065700.parquet", "lg")}</span>')}</div>
 <div class="row">{n(4, f'<span class="row" style="margin:0">{c("log.min_interval")} {inp("0 ms", "sm")} {chk(c, "log.on_change")} {chk(c, "log.raw")}</span>')}</div>
 <div class="row">{n(5, btn(c, "log.stop", "red"))}</div>
 <div class="status">{c("log.active", name="demo_20261023_065700.parquet", rows=18423, size=412)}</div></div>"""


def page_diag(c: Ctx) -> str:
    services = [["ExtendedSession", "10 {session}", "Extended Diagnostic Session"],
                ["ReadVIN", "22 F1 90", "Read VIN"], ["ReadSpeed", "22 F4 0D", "Read vehicle speed"]]
    log = ("12:01:07  &gt;&gt; ReadVIN: 22 F1 90<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&lt;&lt; 62 F1 90 57 44 44 32 30 35 ...   "
           "[" + html.escape(c.t["diag.resp.positive"].format(data="62 F1 90 ...")) + "]<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;VIN: WDD2050421F123456")
    return f"""
<div class="row">{n(1, f'<span class="row" style="margin:0">{c("diag.cdd")} {inp("diag.cdd", "", True)} {c("diag.ecu")} {inp("Engine_ECU", "", True)} {c("diag.request_id")} {inp("7E0", "sm")} {c("diag.response_id")} {inp("7E8", "sm")} {chk(c, "diag.extended")} {chk(c, "diag.fd")} {c("diag.padding")} {inp("AA", "sm")}</span>')}
 {n(2, chk(c, "diag.tester_present"))}</div>
<div class="row" style="margin:6px 0 0"><span class="tab on">{c("diag.sub.services")}</span><span class="tab">{c("diag.sub.vflash")}</span></div>
{n(3, table([c("diag.col.service"), c("diag.col.request"), c("diag.col.description")], services, "", sel={1}))}
<div class="row" style="margin-top:6px">{n(4, table([c("diag.col.param"), c("diag.col.value")], [["did", "F190"]], "", sel=set()))}</div>
<div class="row">{btn(c, "diag.send")} {n(6, inp("22 F1 90", "lg") + " " + btn(c, "diag.send_raw"))}</div>
{n(5, f'<div class="in mono" style="display:block;height:120px;width:100%;padding:6px;line-height:1.6">{log}</div>')}
<div class="row">{btn(c, "diag.clear_log")}</div>"""


def page_vflash(c: Ctx) -> str:
    tree = ('<div class="list" style="height:210px;width:520px;line-height:1.5">'
            f'<div>&#9660; <b>{c("flash.tree.general")}</b></div>'
            '<div style="padding-left:24px">' + html.escape(c.t["flash.info.ecu"]) + ': Engine_ECU</div>'
            '<div style="padding-left:24px">' + html.escape(c.t["flash.info.ids"]) + ': 0x7E0 / 0x7E8</div>'
            f'<div>&#9660; <b>{c("flash.tree.data")}</b></div>'
            '<div style="padding-left:24px">' + html.escape(c.t["flash.block"].format(index=0, address="00010000", length=1200, source="app.hex")) + '</div>'
            '<div style="padding-left:24px">' + html.escape(c.t["flash.block"].format(index=1, address="00020000", length=300, source="cal.bin")) + '</div>'
            f'<div>&#9654; <b>{c("flash.tree.steps")}</b></div></div>')
    steps = [("session", "ok"), ("comm_control", "ok"), ("session", "ok"), ("security", "ok"), ("erase", "ok"),
             ("download", "running"), ("download", "pending"), ("check", "pending"), ("reset", "pending")]
    titles = [c.t["flash.step.session"].format(session="03"), c.t["flash.step.comm_control"].format(control="03"),
              c.t["flash.step.session"].format(session="02"), c.t["flash.step.security"].format(level=1),
              c.t["flash.step.erase"].format(block=0, address="00010000", length=1200),
              c.t["flash.step.download"].format(block=0, address="00010000", length=1200),
              c.t["flash.step.download"].format(block=1, address="00020000", length=300),
              c.t["flash.step.check"].format(routine="FF01"), c.t["flash.step.reset"].format(reset="01")]
    colors = {"ok": "#d7f5d7", "running": "#d6e8fb", "pending": "#ffffff"}
    rows = "".join(f'<tr style="background:{colors[st]}"><td>{i + 1}</td><td>{html.escape(t)}</td>'
                   f'<td>{c("flash.status." + st)}</td></tr>' for i, ((_k, st), t) in enumerate(zip(steps, titles)))
    bar = lambda pct, label="": (f'<div style="height:16px;border:1px solid #999;background:#eee;border-radius:3px;overflow:hidden">'
                                  f'<div style="width:{pct}%;height:100%;background:#2a6ebe"></div></div>{label}')
    dialog = f"""<div style="position:absolute;left:300px;top:70px;width:610px;border:1px solid #666;background:#efefef;box-shadow:4px 6px 14px rgba(0,0,0,.4);padding:8px">
 <div style="background:#dcdcdc;margin:-8px -8px 6px;padding:4px 8px;font-size:12px">{c("flash.dlg.title")}</div>
 <div style="font-weight:600;margin-bottom:4px">{c("flash.dlg.running")}</div>
 <div>{c("flash.dlg.overall")}</div><div data-n="5">{bar(48)}</div><div style="height:6px"></div><div>{bar(55)}</div>
 <div style="color:#444;margin:3px 0">{c("flash.dlg.speed", done="0.6", total="1.2", speed="24.8")}</div>
 <div data-n="4">{'<table class="t" style="font-size:12px"><tr><th>#</th><th>' + c("flash.dlg.col.step") + '</th><th>' + c("flash.dlg.col.status") + '</th></tr>' + rows + '</table>'}</div>
 <div class="row" style="justify-content:flex-end;margin-top:6px">{btn(c, "flash.dlg.abort")} <span class="btn" style="opacity:.5">{c("flash.dlg.close")}</span></div></div>"""
    return f"""<div class="row">{c("diag.cdd")} {inp("diag.cdd", "", True)} {c("diag.ecu")} {inp("Engine_ECU", "", True)} {c("diag.request_id")} {inp("7E0", "sm")} {c("diag.response_id")} {inp("7E8", "sm")}</div>
<div class="row" style="margin-top:0"><span class="tab">{c("diag.sub.services")}</span><span class="tab on" style="border:1px solid #b5b5b5">{c("diag.sub.vflash")}</span></div>
<div class="row">{n(1, btn(c, "flash.load") + " " + btn(c, "flash.reload"))} <span class="status">demo.vflashpack</span>
 <span style="margin-left:auto">{n(3, btn(c, "flash.start"))}</span></div>
<div class="status">{c("flash.loaded", name="Demo Application", blocks=2, kb="1.5", steps=10)}</div>
{n(2, tree)}{dialog}<div style="height:100px"></div>"""


def page_help(c: Ctx) -> str:
    chapters = [c.t.get(f"help.topic.{k}.title", k) for k in
                ("general", "files", "project", "data", "trace", "sequence", "graph", "logging")]
    items = "".join(f'<div class="{"sel" if i == 3 else ""}">{html.escape(t)}</div>' for i, t in enumerate(chapters))
    return f"""<div class="cols"><div style="flex:0 0 300px">{n(1, f'<div class="list" style="height:330px">{items}</div>')}</div>
 <div><div class="row">{n(2, inp(c.t["help.search_placeholder"], "lg"))} {btn(c, "help.next")} {n(4, btn(c, "help.export_pdf"))}</div>
 {n(3, '<div style="border:1px solid #a0a0a0;height:320px;padding:8px;overflow:hidden"><h3 style="margin:0 0 6px">'
    + html.escape(c.t["help.topic.data.title"]) + '</h3><div style="height:12px;background:#eee;margin:5px 0;width:90%"></div>'
    + '<div style="height:12px;background:#eee;margin:5px 0;width:75%"></div><div style="height:110px;background:#f2f2f2;border:1px solid #ddd;margin:8px 0"></div>'
    + '<div style="height:12px;background:#eee;margin:5px 0;width:85%"></div></div>')}</div></div>"""


PAGES = {"project": page_project, "data": page_data, "trace": page_trace, "sequence": page_sequence,
         "graph": page_graph, "graph_multi": page_graph_multi, "builder": page_builder, "trace_custom_edit": page_trace_custom_edit, "builder_signal": page_builder_signal, "builder_scale": page_builder_scale, "builder_range": page_builder_range, "trace_custom": page_trace_custom, "logging": page_logging, "diag": page_diag, "vflash": page_vflash, "help": page_help}


ALIAS = {"header": "project", "vflash": "diag", "graph_multi": "graph", "trace_custom": "trace", "trace_custom_edit": "trace",
         "builder_signal": "builder", "builder_scale": "builder", "builder_range": "builder"}


def window(c: Ctx, tab: str, shot: str) -> str:
    active = ALIAS.get(tab, tab)
    tabs = "".join(f'<span class="tab {"on" if k == active else ""}">{c("tab." + k)}</span>'
                   for k in ("project", "data", "trace", "sequence", "graph", "builder", "logging", "diag", "help"))
    corner_nums = (1, 2) if shot == "header" else (None, None)
    corner = (f'<div class="corner">{n(corner_nums[0], chk(c, "header.dark"))}'
              f'{c("header.language")} {n(corner_nums[1], inp(c.t["meta.name"], "", True))}</div>')
    body = "" if shot == "header" else (page_vflash(c) if shot == "vflash" else PAGES[shot if shot in ALIAS else tab](c))
    if shot == "header":
        return f'<div class="win"><div class="title">{c("app.title")}</div><div class="tabs">{tabs}{corner}</div></div>'
    return (f'<div class="win"><div class="title">{c("app.title")}</div><div class="tabs">{tabs}{corner}</div>'
            f'<div class="page">{body}</div></div>')


def build_html(texts: dict[str, str], lang: str, shot: str) -> str:
    c = Ctx(texts)
    tab = ALIAS.get(shot, shot)
    lang_attr = {"zh": "zh-CN", "th": "th"}.get(lang, lang)
    return (f'<!doctype html><html lang="{lang_attr}"><meta charset="utf-8"><style>{CSS}</style><body>'
            f'{window(c, tab, shot)}<script>{JS}</script></body></html>')


def main(argv: list[str]) -> int:
    from playwright.sync_api import sync_playwright
    tr_ = Translator()
    codes = argv or [code for code, _n in tr_.available()]
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 700})
        for code in codes:
            texts = dict(tr_.texts(code))
            texts["meta.name"] = str(tr_._load(code)[0].get("name", code))
            for shot in SHOTS:
                page.set_content(build_html(texts, code, shot))
                page.wait_for_timeout(60)
                box = page.locator(".win").bounding_box()
                page.screenshot(path=str(OUT / f"{shot}_{code}.png"),
                                clip={"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"] + 2})
            print("ok", code)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
