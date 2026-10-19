"""tide vr spike: answers the questions in design/steamvr.md (vr 0) before
any plugin code gets written. run it through ./run.sh, which sets up
pyopenvr. checklist.md says what to do in the headset and what to note.

    ./run.sh check                      what steamvr reports about your setup
    ./run.sh png [file.png]             a static picture floating in front of you
    ./run.sh panel                      a live qt panel: laser click, hold, drag
    ./run.sh panel --attach left        the same panel riding on your left hand
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
    for _ in range(30):         # a few ticks so action state is live
        s.update()
        time.sleep(0.02)
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
    and the stick triple-click (brings the panel back in front of you)."""

    def __init__(self, s, widget, *, key: str, attach: str | None, flip: bool) -> None:
        from PySide6.QtCore import QTimer, Qt
        from panels import Forwarder, H, W
        from vrcore import Overlay, Stats
        self.s, self.w, self.attach, self.flip = s, widget, attach, flip
        self.W, self.H = W, H
        self.ov = Overlay(s, key, f"tide spike {key}", 0.40 if attach is None else 0.22)
        self.ov.mouse_input(W, H)
        self.fwd = Forwarder(widget)
        self.dirty = True
        widget.dirty.connect(self._mark)
        self.tick_ms, self.upload_ms = Stats(), Stats()
        self.triple = TripleClick()
        self.sticks = {side: False for side in SIDES}
        self.focus_seen: list[str] = []
        self.on_tick = None
        self._last_tick = self._last_upload = self._last_status = time.perf_counter()
        self.hz = s.display_hz()
        widget.show()           # WA_DontShowOnScreen keeps it off the desktop
        self.respawn()
        self.ov.show()
        self.timer = QTimer()
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
                self._log("scroll")
            self.dirty = True

        sticks = {side: self.s.stick(side) for side in SIDES}
        for side in SIDES:
            if sticks[side] != self.sticks[side]:
                self._log(f"stick {side[0]} {'down' if sticks[side] else 'up'}")
        self.sticks = sticks
        if self.triple.feed(Sample(now, None, {}, sticks)):
            self._log("stick triple-click: panel back in front of you")
            for side in SIDES:
                self.s.buzz(side, 0.08, 120, 0.9)
            self.respawn()

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
                mode = {True: "raw", False: "png", None: "?"}[self.ov.raw_ok]
                self.w.stats_label.setText(
                    f"tick p95 {t.get('p95', 0)} ms · upload p50 {u.get('p50', 0)} "
                    f"p95 {u.get('p95', 0)} ms · {mode}")
            self.dirty = True

        if self.dirty and now - self._last_upload >= 1 / 30:
            img = QImage(self.W, self.H, QImage.Format_RGBA8888)
            img.fill(Qt.transparent)
            self.w.render(img)
            self.upload_ms.add(self.ov.upload(img))
            self._last_upload = now
            self.dirty = False

    def report(self) -> dict:
        return {
            "display_hz": self.hz,
            "tick_interval_ms": self.tick_ms.summary(),
            "upload_ms": self.upload_ms.summary(),
            "upload_mode": {True: "raw", False: "png file", None: "none"}[self.ov.raw_ok],
            "raw_error": self.ov.raw_error,
            "input": "ok" if self.s.input is not None else f"failed: {self.s.input_error}",
            "focus_apps_seen": self.focus_seen,
            "attach": self.attach,
            "y_flipped": self.flip,
        }


def _hidden(widget) -> None:
    from PySide6.QtCore import Qt
    widget.setAttribute(Qt.WA_DontShowOnScreen, True)


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
    s = Session()
    loop = PanelLoop(s, panel, key="panel", attach=args.attach, flip=not args.no_flip)
    panel.log("point the laser here and pull the trigger")
    if s.input is None:
        panel.log(f"input failed: {s.input_error}")
    app.exec()
    report = loop.report()
    report["clicks"] = panel.clicks
    print(json.dumps(report, indent=2))
    _write_json(out_dir() / f"panel-{time.strftime('%Y%m%d-%H%M%S')}.json", report)
    loop.ov.destroy()
    s.shutdown()


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
        ov = Overlay(s, f"ring{i}", f"tide spike ring {i}", width_m)
        ov.flag(openvr.VROverlayFlags_SideBySide_Crossed if args.crossed
                else openvr.VROverlayFlags_SideBySide_Parallel)
        ms = ov.upload(img)
        print(f"ring texture {i}: {img.width()}x{img.height()} in {ms:.0f} ms "
              f"({'raw' if ov.raw_ok else 'png file'})")
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
              "upload": "raw" if overlays and overlays[0].raw_ok else "png file",
              "outline_from_headset": {n: e["from_headset"] for n, e in eyes.items()}}
    print(json.dumps(report, indent=2))
    _write_json(out_dir() / f"ring-{time.strftime('%Y%m%d-%H%M%S')}.json", report)
    for ov in overlays:
        ov.destroy()
    s.shutdown()


# ---------- record / replay ----------

def cmd_record(args) -> None:
    from vrcore import Session, out_dir
    s = Session()
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
                sample = Sample(now, s.hmd(), hands, {side: s.stick(side) for side in SIDES})
                f.write(json.dumps(sample.to_json()) + "\n")
                count += 1
                if now - last_focus >= 5.0:
                    last_focus = now
                    f.write(json.dumps({"focus": s.focus_app(), "at": round(now - t0, 2)}) + "\n")
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
        loop = PanelLoop(session, panel, key="voice", attach=None, flip=not args.no_flip)
        panel.log("point at the mic button, hold the trigger, talk, let go")
        app.exec()
        loop.ov.destroy()
        session.shutdown()
    if state["talking"]:
        mic.stop()
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

def main() -> None:
    ap = argparse.ArgumentParser(prog="run.sh", description="tide vr spike (design/steamvr.md, vr 0)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="what steamvr reports about your setup").set_defaults(func=cmd_check)

    p = sub.add_parser("png", help="a static picture in front of you")
    p.add_argument("file", nargs="?")
    p.add_argument("--seconds", type=float, default=60)
    p.set_defaults(func=cmd_png)

    p = sub.add_parser("panel", help="live qt panel with laser input")
    p.add_argument("--attach", choices=SIDES, help="ride on a controller instead of floating")
    p.add_argument("--no-flip", action="store_true",
                   help="use if clicks land mirrored top to bottom")
    p.add_argument("--preview", action="store_true", help="desktop window, no vr")
    p.add_argument("--snapshot", metavar="PNG", help="render the panel to a file, no vr")
    p.set_defaults(func=cmd_panel)

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
    p.set_defaults(func=cmd_ring)

    p = sub.add_parser("record", help="record poses with live detectors")
    p.add_argument("--label", required=True, help="e.g. headphones, wrist-tap, sticks, play")
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--delay", type=int, default=10, help="countdown before recording")
    p.add_argument("--hz", type=float, help="default: the headset's refresh rate")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("replay", help="run the detectors over recordings")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("voice", help="record the 20 spoken requests")
    p.add_argument("--desktop", action="store_true", help="a desktop window, no vr")
    p.add_argument("--device", help="parec source name (default: system default input)")
    p.add_argument("--prompts", default=str(HERE / "prompts.txt"))
    p.add_argument("--no-flip", action="store_true")
    p.set_defaults(func=cmd_voice)

    p = sub.add_parser("score", help="transcribe the clips and search")
    p.add_argument("--clips", help="clip folder (default: ~/.cache/tide-vr-spike/voice)")
    p.set_defaults(func=cmd_score)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
