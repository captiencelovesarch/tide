"""tide vr spike: answers the questions in design/steamvr.md (vr 0) before
any plugin code gets written. run it through ./run.sh, which sets up
pyopenvr. checklist.md says what to do in the headset and what to note.

    ./run.sh check                      what steamvr reports about your setup
    ./run.sh png [file.png]             a static picture floating in front of you
    ./run.sh panel                      a live qt panel: laser click, hold, drag
    ./run.sh panel --attach left        the same panel riding on your left hand
    ./run.sh watch                      a watch on your wrist that opens the panel
    ./run.sh ring                       the lens-shaped ring, pulsing at 120 bpm
    ./run.sh record --label headphones  record poses (+ live gesture detectors)
    ./run.sh replay [files or dirs]     run the detectors over recordings
    ./run.sh voice                      push-to-talk clips of 20 requests
    ./run.sh score                      transcribe the clips, search, count hits

no-headset modes for checking the plumbing:
    ./run.sh panel --preview            the panel as a desktop window
    ./run.sh voice --desktop            record the clips at your desk

output lands in ~/.cache/tide-vr-spike/.
"""
from __future__ import annotations

import argparse
import faulthandler
import json
import math
import random
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from gestures import DETECTORS, SIDES, Hand, Sample, TripleClick, replay  # noqa: E402

# legacy controller-state bits that could be a stick click on index
# (k_EButton_Axis0..Axis4); the panel logs raw bits so the real one shows
LEGACY_STICK_BITS = sum(1 << b for b in range(32, 37))

# a native crash (vrclient, qt teardown) prints the python stack too
faulthandler.enable()


def _qt_app():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    # ctrl-c in the terminal ends the run (qt would swallow it otherwise;
    # the vr tick timer gives python a chance to run the handler)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    return app


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"wrote {path}")


# ---------- check ----------

def cmd_check(args) -> None:
    import openvr
    from vrcore import Session, out_dir, to_np
    s = Session()
    # the first time steamvr sees our action manifest it takes a few seconds
    # to load the default index binding. wait for finger data on every hand
    # that's switched on, up to args.wait seconds.
    print(f"waiting up to {args.wait:.0f} s for controller input", end="", flush=True)
    deadline = time.monotonic() + args.wait
    while True:
        s.update()
        hands = [side for side in SIDES if s.controller_index(side) is not None]
        ready = all(s.curls(side) is not None for side in hands)
        if (ready and hands) or time.monotonic() >= deadline or s.input is None:
            break
        print(".", end="", flush=True)
        time.sleep(0.25)
    print()
    report = {
        "hmd": s.hmd_model(),
        "display_hz": s.display_hz(),
        "input": "ok" if s.input is not None else f"failed: {s.input_error}",
        "focus_app": s.focus_app(),
        "controllers": {},
        "eyes": {},
    }
    for side in SIDES:
        idx = s.controller_index(side)
        if idx is None:
            report["controllers"][side] = None
            continue
        prop = s.system.getStringTrackedDeviceProperty
        report["controllers"][side] = {
            "model": prop(idx, openvr.Prop_ModelNumber_String),
            "type": prop(idx, openvr.Prop_ControllerType_String),
            "finger_curl": s.curls(side),
            "bone_tips": s.bone_tips(side),
            "stick_clicked": s.stick(side),
        }
    for side in SIDES:
        if report["controllers"][side] is None:
            report["controllers"][side] = "not found: switched off, asleep, or out of view?"
    report["input_errors"] = dict(s.input_errors)
    for eye, name in ((openvr.Eye_Left, "left"), (openvr.Eye_Right, "right")):
        mesh = s.system.getHiddenAreaMesh(eye, openvr.k_eHiddenAreaMesh_LineLoop)
        report["eyes"][name] = {
            "outline_points": int(mesh.unTriangleCount),
            "projection_raw": [round(v, 4) for v in s.system.getProjectionRaw(eye)],
            "eye_x": round(float(to_np(s.system.getEyeToHeadTransform(eye))[0, 3]), 4),
        }
    print(json.dumps(report, indent=2))
    _write_json(out_dir() / "check.json", report)
    s.shutdown()


# ---------- png ----------

def _card_png() -> Path:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    img = QImage(800, 400, QImage.Format_RGBA8888)
    img.fill(QColor("#101010"))
    p = QPainter(img)
    p.setPen(QColor("#ffd400"))
    f = QFont("monospace")
    f.setPixelSize(96)
    f.setBold(True)
    p.setFont(f)
    p.drawText(img.rect(), Qt.AlignCenter, "tide")
    p.end()
    path = Path("/tmp/tide-spike-card.png")
    img.save(str(path))
    return path


def cmd_png(args) -> None:
    _qt_app()
    from vrcore import Overlay, Session, spawn_in_front
    path = Path(args.file) if args.file else _card_png()
    s = Session(with_input=False)
    s.update()
    hmd = s.hmd()
    if hmd is None:
        raise SystemExit("no headset pose yet. put it on and try again")
    ov = Overlay(s, "png", "tide spike png", 0.4)
    ov.from_file(path)
    ov.place(spawn_in_front(hmd))
    ov.show()
    print(f"showing {path} for {args.seconds} s (ctrl-c ends it)")
    end = time.monotonic() + args.seconds
    try:
        while time.monotonic() < end and not s.quit:
            s.update()
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    ov.destroy()
    s.shutdown()


# ---------- panel (and the loop voice shares) ----------

class PanelLoop:
    """one hidden qt panel in vr: placement, laser input, redraws, stats,
    and the stick triple-click (closes the panel, or opens it in front of
    you, the summon backup from design/steamvr.md)."""

    def __init__(self, s, widget, *, key: str, attach: str | None, invert_clicks: bool = False,
                 upload: str = "gl", gl_flip: bool = True, start_open: bool = True) -> None:
        from PySide6.QtCore import QTimer, Qt
        from panels import Forwarder, H, W
        from vrcore import Overlay, Stats
        # steamvr reports laser y bottom-up in the texture's own frame. a
        # texture drawn with flipped bounds flips that frame too, so its
        # clicks arrive top-down already (2026-10-19: flipping them again
        # sent every click off the buttons; only the slider, at mid
        # height, still worked)
        flip = not (upload == "gl" and gl_flip)
        self.s, self.w, self.attach = s, widget, attach
        self.flip = flip != invert_clicks
        self.W, self.H = W, H
        self.ov = Overlay(s, key, f"tide spike {key}", 0.40 if attach is None else 0.22,
                          upload=upload)
        self.ov.gl_flip = gl_flip
        self.upload_errors: list[str] = []
        self.uploads_stopped = ""
        self.ov.mouse_input(W, H)
        self.fwd = Forwarder(widget)
        self.dirty = True
        widget.dirty.connect(self._mark)
        self.tick_ms, self.upload_ms = Stats(), Stats()
        self.triple = TripleClick()
        self.visible = start_open
        self.legacy: dict[str, int | None] = {side: None for side in SIDES}
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.stick_sources_seen = {"action": False, "legacy": False}
        self._said_active = False
        self._scrolls = 0
        self._last_scroll = 0.0
        self._started = time.perf_counter()
        self.sticks = {side: False for side in SIDES}
        self.action_sticks = {side: False for side in SIDES}
        self.focus_seen: list[str] = []
        self.on_tick = None
        self._last_tick = self._last_upload = self._last_status = time.perf_counter()
        self.hz = s.display_hz()
        widget.show()           # WA_DontShowOnScreen keeps it off the desktop
        if start_open:
            self.respawn()
            self.ov.show()
        if hasattr(widget, "close_requested"):
            widget.close_requested.connect(lambda: self.close_panel("x"))
        self.timer = QTimer(widget)     # parented: never outlives the panel
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(max(1, int(1000 / self.hz)))

    def _mark(self) -> None:
        self.dirty = True

    def respawn(self) -> None:
        from vrcore import rot_x, spawn_in_front, translate
        if self.attach:
            idx = self.s.controller_index(self.attach)
            if idx is None:
                print(f"no {self.attach} controller found")
                return
            # above the controller, tilted back toward your face
            self.ov.place_on(idx, translate(0.0, 0.08, 0.03) @ rot_x(-55))
            return
        self.s.update()
        hmd = self.s.hmd()
        if hmd is not None:
            self.ov.place(spawn_in_front(hmd))

    def toggle(self) -> None:
        """the stick triple-click (which never reaches an overlay, see the
        spike findings; kept for the record)."""
        if self.visible:
            self.close_panel("stick triple-click")
        else:
            self.open_panel("stick triple-click")

    def open_panel(self, how: str, at=None) -> None:
        """show it: in front of you, or at a given pose."""
        if self.visible:
            return
        self.visible = True
        if at is not None:
            self.ov.place(at)
        else:
            self.respawn()
        self.ov.show()
        self.dirty = True
        for side in SIDES:
            self.s.buzz(side, 0.08, 120, 0.9)
        self.opened.append(how)
        self._log(f"panel opened by {how}")

    def close_panel(self, how: str) -> None:
        if not self.visible:
            return
        self.visible = False
        self.ov.hide()
        self.fwd.leave()
        # two short buzzes for closing, one long for opening
        for side in SIDES:
            self.s.buzz(side, 0.03, 200, 0.7)
        self.closed.append(how)
        print(f"{time.strftime('%H:%M:%S')} panel closed by {how}", flush=True)

    def _log(self, text: str) -> None:
        log = getattr(self.w, "log", None)
        if log:
            log(text)

    def tick(self) -> None:
        import openvr
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication
        now = time.perf_counter()
        self.tick_ms.add((now - self._last_tick) * 1000.0)
        self._last_tick = now
        self.s.update()
        if self.s.quit:
            QApplication.quit()
            return

        for kind, x, y, _button in self.ov.events():
            qy = self.H - y if self.flip else y
            if kind == openvr.VREvent_MouseMove:
                self.fwd.move(x, qy)
            elif kind == openvr.VREvent_MouseButtonDown:
                self.fwd.move(x, qy)
                self.fwd.press()
                self._log(f"laser down at {x:.0f},{qy:.0f}")
            elif kind == openvr.VREvent_MouseButtonUp:
                self.fwd.release()
                self._log("laser up")
            elif kind == openvr.VREvent_ScrollSmooth:
                # the stick sends dozens a second; one line per burst
                self._scrolls += 1
                self._last_scroll = now
            self.dirty = True

        if self._scrolls and now - self._last_scroll > 0.3:
            self._log(f"scroll ×{self._scrolls}")
            self._scrolls = 0
        if not self._said_active and now - self._started >= 2.0:
            self._said_active = True
            active = {side: self.s.stick_active(side) for side in SIDES}
            self._log("stick actions: " + ", ".join(
                f"{side[0]} {'active' if a else 'inactive' if a is False else 'n/a'}"
                for side, a in active.items()))
        action = {side: self.s.stick(side) for side in SIDES}
        sticks = dict(action)
        for side in SIDES:
            if action[side] != self.action_sticks[side]:
                self._log(f"stick {side[0]} {'down' if action[side] else 'up'} (action)")
                self.stick_sources_seen["action"] = True
            bits = self.s.legacy_buttons(side)
            if bits is not None and self.legacy[side] is not None and bits != self.legacy[side]:
                self._log(f"legacy {side[0]} buttons {bits:#x}")
                self.stick_sources_seen["legacy"] = True
            self.legacy[side] = bits
            # whichever source works counts for the triple-click; the log
            # says which one it was
            sticks[side] = sticks[side] or bool((bits or 0) & LEGACY_STICK_BITS)
        self.sticks = sticks
        self.action_sticks = action
        if self.triple.feed(Sample(now, None, {}, sticks)):
            self.toggle()

        if self.on_tick is not None:
            self.on_tick()

        if now - self._last_status >= 1.0:
            self._last_status = now
            focus = self.s.focus_app()
            if focus not in self.focus_seen:
                self.focus_seen.append(focus)
            if hasattr(self.w, "focus_label"):
                self.w.focus_label.setText(f"focus: {focus}")
            if hasattr(self.w, "stats_label"):
                t, u = self.tick_ms.summary(), self.upload_ms.summary()
                self.w.stats_label.setText(
                    f"tick p95 {t.get('p95', 0)} ms · upload p50 {u.get('p50', 0)} "
                    f"p95 {u.get('p95', 0)} ms · {self.ov.mode}")
            self.dirty = True

        from vrcore import MAX_FPS, STALL_S
        if (self.dirty and self.visible and not self.uploads_stopped
                and now - self._last_upload >= 1 / MAX_FPS[self.ov.mode]):
            img = QImage(self.W, self.H, QImage.Format_RGBA8888)
            img.fill(Qt.transparent)
            self.w.render(img)
            try:
                ms = self.ov.upload(img)
            except Exception as exc:  # noqa: BLE001 - any upload failure is a finding
                # no fallback to another mode: falling back to files is
                # what took the compositor down on 2026-10-19
                self._stop_uploads(f"{self.ov.mode} upload failed: {type(exc).__name__}: {exc}")
            else:
                self.upload_ms.add(ms)
                if ms / 1000.0 > STALL_S:
                    self._stop_uploads(f"an upload took {ms:.0f} ms: steamvr's compositor "
                                       "stopped answering (crashed?)")
                    QApplication.quit()
            self._last_upload = now
            self.dirty = False

    def _stop_uploads(self, why: str) -> None:
        self.uploads_stopped = why
        self.upload_errors.append(why)
        print(f"stopping uploads: {why}", flush=True)
        self._log("uploads stopped, see terminal")

    def close(self) -> None:
        """stop ticking and take the overlay down, before openvr shuts down."""
        self.timer.stop()
        self.ov.destroy()

    def report(self) -> dict:
        return {
            "display_hz": self.hz,
            "tick_interval_ms": self.tick_ms.summary(),
            "upload_ms": self.upload_ms.summary(),
            "upload_mode": self.ov.mode,
            "uploads_stopped": self.uploads_stopped,
            "upload_errors": self.upload_errors[:5],
            "gl": self.s.gl.info if self.s.gl is not None else None,
            "input": "ok" if self.s.input is not None else f"failed: {self.s.input_error}",
            "focus_apps_seen": self.focus_seen,
            "stick_sources_seen": self.stick_sources_seen,
            "opened_by": self.opened,
            "closed_by": self.closed,
            "global_input": self.s.global_input,
            "attach": self.attach,
            "y_flipped": self.flip,
        }


def _hidden(widget) -> None:
    from PySide6.QtCore import Qt
    widget.setAttribute(Qt.WA_DontShowOnScreen, True)


def _teardown(loop, session, widget) -> None:
    """the panel run once segfaulted in pyside's exit cleanup, after
    openvr.shutdown(). so: overlays down, openvr off, then our qt objects
    deleted by hand while the app still exists. each step prints, so if it
    still crashes the last line says where."""
    import shiboken6
    from PySide6.QtWidgets import QApplication
    steps = (
        ("overlay down", loop.close if loop is not None else None),
        ("openvr shutdown", session.shutdown if session is not None else None),
        ("qt objects deleted", lambda: shiboken6.isValid(widget) and shiboken6.delete(widget)),
        ("qt events drained", QApplication.processEvents),
    )
    for name, step in steps:
        if step is None:
            continue
        step()
        print(f"teardown: {name}", flush=True)


def cmd_panel(args) -> None:
    app = _qt_app()
    from panels import SpikePanel
    panel = SpikePanel()
    if args.snapshot:
        _hidden(panel)
        panel.show()
        panel.grab().save(args.snapshot)
        print(f"wrote {args.snapshot}")
        return
    if args.preview:
        panel.show()
        app.exec()
        return
    from paths import out_dir
    from vrcore import Session
    _hidden(panel)
    s = Session(global_input=args.global_input)
    if args.upload in ("raw", "file"):
        print(f"warning: {args.upload} uploads crashed vrcompositor before. re-test only")
    loop = PanelLoop(s, panel, key="panel", attach=args.attach, invert_clicks=args.invert_clicks,
                     upload=args.upload, gl_flip=not args.no_gl_flip)
    panel.log("point the laser here and pull the trigger")
    if s.input is None:
        panel.log(f"input failed: {s.input_error}")
    app.exec()
    report = loop.report()
    report["clicks"] = panel.clicks
    print(json.dumps(report, indent=2))
    _write_json(out_dir() / f"panel-{time.strftime('%Y%m%d-%H%M%S')}.json", report)
    _teardown(loop, s, panel)


# ---------- watch ----------

def cmd_watch(args) -> None:
    app = _qt_app()
    from panels import SpikePanel
    from paths import out_dir
    from vrcore import Session
    from watch import Watch
    panel = SpikePanel()
    _hidden(panel)
    s = Session()
    loop = PanelLoop(s, panel, key="panel", attach=None, start_open=False)
    watch = Watch(s, loop, args.hand)
    loop.on_tick = watch.tick
    print(f"raise your {args.hand} wrist and look at it. poke the watch with your other "
          "index finger or laser it. the x closes the panel. ctrl-c ends it", flush=True)
    app.exec()
    report = loop.report() | watch.report()
    print(json.dumps(report, indent=2))
    _write_json(out_dir() / f"watch-{time.strftime('%Y%m%d-%H%M%S')}.json", report)
    watch.ov.destroy()
    _teardown(loop, s, panel)


# ---------- ring ----------

def _outline_uv(s, eye) -> list[tuple[float, float]]:
    import openvr
    mesh = s.system.getHiddenAreaMesh(eye, openvr.k_eHiddenAreaMesh_LineLoop)
    n = int(mesh.unTriangleCount)
    if not n or not mesh.pVertexData:
        return []
    return [(float(mesh.pVertexData[i].v[0]), float(mesh.pVertexData[i].v[1])) for i in range(n)]


def _ellipse_uv(n: int = 96) -> list[tuple[float, float]]:
    return [(0.5 + 0.47 * math.cos(2 * math.pi * i / n), 0.5 + 0.47 * math.sin(2 * math.pi * i / n))
            for i in range(n)]


def _ring_images(eyes: dict, args) -> tuple[list, float]:
    """one white side-by-side texture per grain variant, plus the quad width
    in meters. each eye's half carries that eye's outline, mapped from its
    viewport uv through its own projection onto a quad ``distance`` m out."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF

    d = args.distance

    def to_quad(e, u, v):
        left, right, top, bottom = e["proj"]
        hi, lo = max(top, bottom), min(top, bottom)
        if args.flip_y:
            v = 1.0 - v
        tan_x = left + u * (right - left)
        tan_up = hi - v * (hi - lo)        # viewport v grows downward
        return e["eye_x"] + tan_x * d, tan_up * d

    quad = {name: [to_quad(e, u, v) for u, v in e["uv"]] for name, e in eyes.items()}
    centers = {name: to_quad(e, *_center_uv(e)) for name, e in eyes.items()}
    xs = [abs(x) for pts in quad.values() for x, _ in pts]
    ys = [abs(y) for pts in quad.values() for _, y in pts]
    qw, qh = 2 * max(xs) * 1.04, 2 * max(ys) * 1.04

    half_w = 960
    # does steamvr size a side-by-side quad from half the texture or all of
    # it? unknown until seen; --aspect picks, the guide line shows which.
    tex_h = half_w * qh / qw * (2 if args.aspect == "full" else 1)
    if tex_h > 1080:            # setOverlayFromFile's limit
        half_w = int(half_w * 1080 / tex_h)
        tex_h = 1080
    tex_w, tex_h = half_w * 2, int(round(tex_h))

    def px(x, y, offset):
        return QPointF(offset + (x + qw / 2) / qw * half_w, (qh / 2 - y) / qh * tex_h)

    order = ("right", "left") if args.crossed else ("left", "right")
    images = []
    for variant in range(args.grain):
        rng = random.Random(variant)
        img = QImage(tex_w, tex_h, QImage.Format_RGBA8888)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        for slot, name in enumerate(order):
            off = slot * half_w
            pts = [px(x, y, off) for x, y in quad[name]]
            cx = px(*centers[name], off)
            path = QPainterPath()
            path.addPolygon(QPolygonF(pts))
            path.closeSubpath()
            p.save()
            p.setClipPath(path)
            # the glow: wide soft strokes clipped to the inside of the edge
            for width, alpha in ((90, 18), (60, 28), (36, 45), (18, 70)):
                p.setPen(QPen(QColor(255, 255, 255, alpha), width))
                p.drawPath(path)
            # the grain: dots near the edge, thinning out toward the middle
            p.setPen(Qt.NoPen)
            for _ in range(5000):
                j = rng.randrange(len(pts))
                a, b = pts[j], pts[(j + 1) % len(pts)]
                t = rng.random()
                ex, ey = a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t
                depth = rng.random() ** 2.2 * 0.16
                gx, gy = ex + (cx.x() - ex) * depth, ey + (cx.y() - ey) * depth
                p.setBrush(QColor(255, 255, 255, rng.randrange(120, 256)))
                r = rng.choice((1.0, 1.0, 1.5, 2.0))
                p.drawEllipse(QPointF(gx, gy), r, r)
            p.restore()
            if not args.no_guide:
                # exactly on the outline, and a cross where that eye looks
                # straight ahead: if these sit right, the mapping is right
                p.setPen(QPen(QColor(255, 255, 255, 255), 2))
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)
                p.drawLine(QPointF(cx.x() - 14, cx.y()), QPointF(cx.x() + 14, cx.y()))
                p.drawLine(QPointF(cx.x(), cx.y() - 14), QPointF(cx.x(), cx.y() + 14))
        p.end()
        images.append(img)
    return images, qw


def _center_uv(e) -> tuple[float, float]:
    """the viewport uv where that eye looks straight ahead (tan 0, 0)."""
    left, right, top, bottom = e["proj"]
    hi, lo = max(top, bottom), min(top, bottom)
    return -left / (right - left), hi / (hi - lo)


def cmd_ring(args) -> None:
    _qt_app()
    import openvr
    from vrcore import Overlay, Session, Stats, out_dir, to_np, translate
    s = Session(with_input=False)
    eyes = {}
    for eye, name in ((openvr.Eye_Left, "left"), (openvr.Eye_Right, "right")):
        uv = _outline_uv(s, eye)
        eyes[name] = {
            "uv": uv or _ellipse_uv(),
            "from_headset": bool(uv),
            "proj": list(s.system.getProjectionRaw(eye)),
            "eye_x": float(to_np(s.system.getEyeToHeadTransform(eye))[0, 3]),
        }
    _write_json(out_dir() / "ring-geometry.json",
                {"hmd": s.hmd_model(), "args": vars(args) | {"func": None}, "eyes": eyes})
    images, width_m = _ring_images(eyes, args)

    overlays = []
    for i, img in enumerate(images):
        # static images, uploaded once each. gl by default: four big png
        # loads at once is the burst that wrapped the compositor's ring
        ov = Overlay(s, f"ring{i}", f"tide spike ring {i}", width_m, upload=args.upload)
        ov.gl_flip = not args.no_gl_flip
        ov.flag(openvr.VROverlayFlags_SideBySide_Crossed if args.crossed
                else openvr.VROverlayFlags_SideBySide_Parallel)
        ms = ov.upload(img)
        print(f"ring texture {i}: {img.width()}x{img.height()} in {ms:.0f} ms "
              f"({args.upload})")
        ov.place_on(openvr.k_unTrackedDeviceIndex_Hmd, translate(0, 0, -args.distance))
        r, g, b = (int(args.color[k:k + 2], 16) / 255 for k in (1, 3, 5))
        ov.color(r, g, b)
        ov.alpha(0.0)
        ov.show()
        overlays.append(ov)
    print(f"ring up: {width_m:.2f} m wide at {args.distance} m, {args.bpm} bpm. ctrl-c ends it")

    beat = 60.0 / args.bpm
    call_ms = Stats()
    t0 = time.perf_counter()
    end = t0 + args.seconds
    hz = s.display_hz()
    try:
        while time.perf_counter() < end and not s.quit:
            s.update()
            t = time.perf_counter() - t0
            kick = math.exp(-(t % beat) / 0.16)
            level = 0.3 + 0.7 * kick
            # cross-fade the grain variants so the speckle shimmers without
            # ever re-uploading a texture
            phase = (t * 3.0) % len(overlays)
            c0 = time.perf_counter()
            for i, ov in enumerate(overlays):
                dist = min(abs(phase - i), len(overlays) - abs(phase - i))
                weight = max(0.0, 1.0 - dist) if len(overlays) > 1 else 1.0
                ov.alpha(level * weight)
            call_ms.add((time.perf_counter() - c0) * 1000.0)
            time.sleep(max(0.0, 1.0 / hz - (time.perf_counter() - c0)))
    except KeyboardInterrupt:
        pass
    report = {"alpha_calls_per_tick_ms": call_ms.summary(), "overlays": len(overlays),
              "upload": args.upload,
              "gl": s.gl.info if s.gl is not None else None,
              "outline_from_headset": {n: e["from_headset"] for n, e in eyes.items()}}
    print(json.dumps(report, indent=2))
    _write_json(out_dir() / f"ring-{time.strftime('%Y%m%d-%H%M%S')}.json", report)
    for ov in overlays:
        ov.destroy()
    s.shutdown()


# ---------- record / replay ----------

def cmd_record(args) -> None:
    from vrcore import Session, out_dir
    s = Session(global_input=args.global_input)
    if s.input is None:
        print(f"warning: input failed ({s.input_error}). no finger curl or stick clicks")
    hz = args.hz or s.display_hz()
    path = out_dir("poses") / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.label}.jsonl"
    detectors = [d() for d in DETECTORS]
    fires: dict[str, list[float]] = {d.name: [] for d in detectors}

    for n in range(args.delay, 0, -1):
        print(f"recording '{args.label}' in {n}…", flush=True)
        s.update()
        time.sleep(1)
    for side in SIDES:
        s.buzz(side, 0.25, 100, 0.8)
    print(f"recording for {args.seconds} s at {hz:.0f} hz → {path}")

    count = 0
    with_curl = {side: 0 for side in SIDES}
    with_pose = {side: 0 for side in SIDES}
    focus_seen: list[str] = []
    t0 = time.perf_counter()
    next_t = t0
    last_focus = 0.0
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"label": args.label, "hmd": s.hmd_model(), "hz": hz,
                            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "input": s.input is not None}) + "\n")
        try:
            while not s.quit:
                now = time.perf_counter()
                if now - t0 >= args.seconds:
                    break
                s.update()
                hands = {}
                for side in SIDES:
                    ctrl = s.controller(side)
                    hands[side] = Hand(
                        pose=ctrl[0] if ctrl else None,
                        velocity=ctrl[1] if ctrl else None,
                        curl=s.curls(side),
                        tips=s.bone_tips(side),
                        apose=s.action_pose(side),
                    )
                sticks = {side: s.stick(side) or bool((s.legacy_buttons(side) or 0) & LEGACY_STICK_BITS)
                          for side in SIDES}
                sample = Sample(now, s.hmd(), hands, sticks)
                f.write(json.dumps(sample.to_json()) + "\n")
                count += 1
                for side in SIDES:
                    if hands[side].curl is not None:
                        with_curl[side] += 1
                    if hands[side].pose is not None:
                        with_pose[side] += 1
                if now - last_focus >= 5.0:
                    last_focus = now
                    focus = s.focus_app()
                    if focus not in focus_seen:
                        focus_seen.append(focus)
                    f.write(json.dumps({"focus": focus, "at": round(now - t0, 2)}) + "\n")
                for det in detectors:
                    if det.feed(sample):
                        at = round(now - t0, 2)
                        fires[det.name].append(at)
                        print(f"  {at:7.2f} s  {det.name}", flush=True)
                        for side in SIDES:
                            s.buzz(side, 0.06, 200, 1.0)
                next_t += 1.0 / hz
                time.sleep(max(0.0, next_t - time.perf_counter()))
        except KeyboardInterrupt:
            pass
    for _ in range(2):
        for side in SIDES:
            s.buzz(side, 0.12, 100, 0.8)
        time.sleep(0.25)
    duration = time.perf_counter() - t0
    print(f"\n{count} samples over {duration:.0f} s ({count / max(duration, 1e-6):.0f} hz)")
    print(f"  focus: {', '.join(focus_seen) or '?'}")
    for side in SIDES:
        n = max(count, 1)
        print(f"  {side} hand: position in {100 * with_pose[side] / n:.0f}% of samples, "
              f"finger tracking in {100 * with_curl[side] / n:.0f}%")
    if s.input_errors:
        print(f"  input errors: {s.input_errors}")
    for name, times in fires.items():
        print(f"  {name}: fired {len(times)}×")
    print(f"saved {path}")
    s.shutdown()


def cmd_replay(args) -> None:
    from paths import OUT
    targets = [Path(p) for p in args.paths] or [OUT / "poses"]
    files = []
    for t in targets:
        files += sorted(t.glob("*.jsonl")) if t.is_dir() else [t]
    if not files:
        raise SystemExit("no recordings found")
    for path in files:
        fires = replay(path)
        summary = "  ".join(f"{name}: {len(times)}" for name, times in fires.items())
        print(f"{path.name}\n  {summary}")
        for name, times in fires.items():
            if times:
                print(f"    {name} at {', '.join(f'{t:.1f}' for t in times)} s")


# ---------- voice ----------

def cmd_voice(args) -> None:
    app = _qt_app()
    from PySide6.QtCore import QTimer
    from panels import VoicePanel
    from paths import out_dir
    from speech import Mic, load_prompts, write_wav

    prompts = load_prompts(Path(args.prompts))
    clips = out_dir("voice")
    panel = VoicePanel()
    mic = Mic(args.device)
    state = {"i": 0, "talking": False}
    saved: dict[int, dict] = {}
    manifest = clips / "clips.json"
    if manifest.exists():
        saved = {c["index"]: c for c in json.loads(manifest.read_text())}
        while state["i"] in saved and state["i"] < len(prompts):
            state["i"] += 1

    session = None
    loop = None

    def buzz() -> None:
        if session is not None:
            for side in SIDES:
                session.buzz(side, 0.04, 180, 0.8)

    def show() -> None:
        i = state["i"]
        if i >= len(prompts):
            panel.say_label.setText("done. take the headset off and run: ./run.sh score")
            panel.mic.setEnabled(False)
        else:
            panel.show_prompt(i, len(prompts), prompts[i].say)
        panel.dirty.emit()

    def started() -> None:
        if state["i"] >= len(prompts):
            return
        mic.start()
        state["talking"] = True
        panel.mic.setText("talking…")
        buzz()
        panel.dirty.emit()

    def stopped() -> None:
        if not state["talking"]:
            return
        state["talking"] = False
        pcm = mic.stop()
        panel.mic.setText("hold to talk")
        buzz()
        seconds = len(pcm) / 32000
        if seconds < 0.4:
            panel.log(f"too short ({seconds:.1f} s), try again")
            return
        i = state["i"]
        name = f"clip_{i + 1:02d}.wav"
        write_wav(clips / name, pcm)
        p = prompts[i]
        saved[i] = {"index": i, "file": name, "say": p.say, "title": p.title, "artist": p.artist,
                    "seconds": round(seconds, 2)}
        manifest.write_text(json.dumps(sorted(saved.values(), key=lambda c: c["index"]), indent=2))
        panel.log(f"saved {name} ({seconds:.1f} s)")
        state["i"] += 1
        show()

    def redo() -> None:
        state["i"] = max(0, state["i"] - 1)
        panel.mic.setEnabled(True)
        panel.log("say that one again")
        show()

    def skip() -> None:
        state["i"] += 1
        show()

    panel.talk_started.connect(started)
    panel.talk_stopped.connect(stopped)
    panel.redo.connect(redo)
    panel.skip.connect(skip)

    ticker = QTimer()
    ticker.timeout.connect(lambda: (panel.mic.setText(f"talking… {mic.seconds:.1f} s"),
                                    panel.dirty.emit()) if state["talking"] else None)
    ticker.start(200)

    show()
    panel.log(f"clips go to {clips}")
    if args.desktop:
        panel.show()
        app.exec()
    else:
        from vrcore import Session
        _hidden(panel)
        session = Session()
        loop = PanelLoop(session, panel, key="voice", attach=None, invert_clicks=args.invert_clicks,
                         upload=args.upload, gl_flip=not args.no_gl_flip)
        panel.log("point at the mic button, hold the trigger, talk, let go")
        app.exec()
    if state["talking"]:
        mic.stop()
    if loop is not None:
        _teardown(loop, session, panel)
    print(f"{len(saved)} clips in {clips}")


def cmd_score(args) -> None:
    from paths import OUT
    from speech import Prompt, ensure_model, rank, read_wav, search, strip_command, transcribe
    clips = Path(args.clips) if args.clips else OUT / "voice"
    manifest = clips / "clips.json"
    if not manifest.exists():
        raise SystemExit(f"no clips at {clips}. run ./run.sh voice first")
    if not ensure_model():
        raise SystemExit("no speech model, nothing to score")
    from ytmusicapi import YTMusic
    yt = YTMusic()      # signed out: search works without an account

    rows = []
    for c in json.loads(manifest.read_text()):
        prompt = Prompt(c["say"], c["title"], c["artist"])
        heard, ms = transcribe(read_wav(clips / c["file"]))
        results = search(yt, strip_command(heard))
        got = rank(prompt, results)
        # the same search with a perfect transcript, so a miss can be
        # blamed on the ears or on the search
        ideal = rank(prompt, search(yt, strip_command(prompt.say)))
        top = f"{results[0][0]} · {', '.join(results[0][1])}" if results else "(nothing)"
        rows.append({"said": c["say"], "heard": heard, "top": top, "rank": got,
                     "rank_if_heard_perfectly": ideal, "decode_ms": round(ms)})
        mark = "✓" if got == 1 else (f"#{got}" if got else "✗")
        print(f"{mark:>3}  said:  {c['say']}\n     heard: {heard}\n     top:   {top}\n")

    n = len(rows)
    top1 = sum(1 for r in rows if r["rank"] == 1)
    top3 = sum(1 for r in rows if r["rank"] and r["rank"] <= 3)
    ideal1 = sum(1 for r in rows if r["rank_if_heard_perfectly"] == 1)
    decode = sorted(r["decode_ms"] for r in rows)
    summary = {"clips": n, "top1": top1, "top3": top3, "top1_with_perfect_transcript": ideal1,
               "decode_ms_median": decode[len(decode) // 2] if decode else None}
    print(f"right song first: {top1}/{n} · in the top 3: {top3}/{n} · "
          f"with a perfect transcript: {ideal1}/{n} · decode {summary['decode_ms_median']} ms median")
    _write_json(clips / "score.json", {"summary": summary, "rows": rows})


# ---------- main ----------

def _global_input_flag(p) -> None:
    p.add_argument("--global-input", action="store_true",
                   help="read buttons while a game has focus. needs steamvr settings → "
                        "developer → experimental overlay input overrides")


def _upload_flags(p) -> None:
    p.add_argument("--upload", choices=("gl", "file", "raw"), default="gl",
                   help="how frames reach steamvr: gl (default, an opengl texture), "
                        "file (png, 1 fps; at 10 fps it crashed the compositor), "
                        "raw (crashed the compositor)")
    p.add_argument("--no-gl-flip", action="store_true",
                   help="with --upload gl: use if the panel shows upside down "
                        "(gl textures are flipped by default)")


def main() -> None:
    ap = argparse.ArgumentParser(prog="run.sh", description="tide vr spike (design/steamvr.md, vr 0)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="what steamvr reports about your setup")
    p.add_argument("--wait", type=float, default=8.0, help="seconds to wait for finger data")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("png", help="a static picture in front of you")
    p.add_argument("file", nargs="?")
    p.add_argument("--seconds", type=float, default=60)
    p.set_defaults(func=cmd_png)

    p = sub.add_parser("panel", help="live qt panel with laser input")
    p.add_argument("--attach", choices=SIDES, help="ride on a controller instead of floating")
    p.add_argument("--invert-clicks", action="store_true",
                   help="use if clicks land mirrored top to bottom")
    _global_input_flag(p)
    p.add_argument("--preview", action="store_true", help="desktop window, no vr")
    p.add_argument("--snapshot", metavar="PNG", help="render the panel to a file, no vr")
    _upload_flags(p)
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("watch", help="a watch on your wrist that opens the panel")
    p.add_argument("--hand", choices=SIDES, default="left", help="which wrist (default left)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("ring", help="the lens-shaped ring")
    p.add_argument("--distance", type=float, default=2.0, help="meters out (default 2)")
    p.add_argument("--bpm", type=float, default=120)
    p.add_argument("--seconds", type=float, default=120)
    p.add_argument("--grain", type=int, default=4, help="grain variants to cross-fade")
    p.add_argument("--color", default="#ffd400")
    p.add_argument("--aspect", choices=("half", "full"), default="half",
                   help="use full if the ring looks squashed top to bottom")
    p.add_argument("--flip-y", action="store_true", help="use if the nose cut shows at the top")
    p.add_argument("--crossed", action="store_true", help="use if each eye shows the other's ring")
    p.add_argument("--no-guide", action="store_true", help="hide the outline line and center cross")
    _upload_flags(p)
    p.set_defaults(func=cmd_ring)

    p = sub.add_parser("record", help="record poses with live detectors")
    p.add_argument("--label", required=True, help="e.g. headphones, wrist-tap, sticks, play")
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--delay", type=int, default=10, help="countdown before recording")
    p.add_argument("--hz", type=float, help="default: the headset's refresh rate")
    _global_input_flag(p)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("replay", help="run the detectors over recordings")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("voice", help="record the 20 spoken requests")
    p.add_argument("--desktop", action="store_true", help="a desktop window, no vr")
    p.add_argument("--device", help="parec source name (default: system default input)")
    p.add_argument("--prompts", default=str(HERE / "prompts.txt"))
    p.add_argument("--invert-clicks", action="store_true",
                   help="use if clicks land mirrored top to bottom")
    _upload_flags(p)
    p.set_defaults(func=cmd_voice)

    p = sub.add_parser("score", help="transcribe the clips and search")
    p.add_argument("--clips", help="clip folder (default: ~/.cache/tide-vr-spike/voice)")
    p.set_defaults(func=cmd_score)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
