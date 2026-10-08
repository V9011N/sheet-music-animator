"""Render the camera's view of the scene to a video file via ffmpeg."""
from __future__ import annotations

import json
import math
import multiprocessing as mp
import os
import queue
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter

from .project import Project
from .scene import SheetScene

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def default_workers() -> int:
    """Processes for a render with effects.  The compositing is limited by memory bandwidth, so beyond about
    four processes more of them only use more RAM."""
    return max(2, min((os.cpu_count() or 4) // 3, 6))


def total_duration(scene: SheetScene, project: Project) -> float:
    return scene.score.duration + project.settings.offset + project.settings.tail


def _paint_scene(scene: SheetScene, image: QImage, pose: tuple):
    """Let the scene draw what the (rotated) camera `pose` = (cx, cy, w, h, rot) sees onto `image`."""
    cx, cy, w, h, rot = pose
    p = QPainter(image)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
    # Map the (rotated) camera window onto the image, then let the scene draw the area it covers.
    p.translate(image.width() / 2, image.height() / 2)
    p.rotate(-rot)
    p.scale(image.width() / w, image.height() / h)
    p.translate(-cx, -cy)
    a = math.radians(rot)
    half_w = abs(w * math.cos(a)) / 2 + abs(h * math.sin(a)) / 2
    half_h = abs(w * math.sin(a)) / 2 + abs(h * math.cos(a)) / 2
    area = QRectF(cx - half_w, cy - half_h, 2 * half_w, 2 * half_h)
    scene.render(p, area, area, Qt.IgnoreAspectRatio)
    p.end()


def render_frame(scene: SheetScene, project: Project, t: float, image: QImage):
    scene.apply_time(t)
    image.fill(QColor(project.settings.paper))
    _paint_scene(scene, image, project.camera_pose(t))


# ====================================================================================== effects frames
def effect_loudness(project: Project, audio_path: str | None):
    """(rms, frames per second) of the recording the effects react to, or None."""
    if not audio_path:
        return None
    from . import analysis
    try:
        return analysis.loudness(audio_path)
    except (OSError, ValueError):
        return None


class EffectsRenderer:
    """Finished frames (numpy RGB) of a project with effects.  The scene's ink is drawn by Qt as an alpha
    mask for the camera of that frame; everything else is made by `effects.Compositor`."""

    def __init__(self, scene: SheetScene, project: Project, W: int, H: int, fps: int, duration: float,
                 loudness=None):
        from .effects import Compositor
        self.scene, self.project, self.W, self.H, self.fps = scene, project, W, H, fps
        self.comp = Compositor(project, scene.score, W, H, fps, duration, loudness)
        self.image = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        self._spots = {lay.id: self._page_range(lay) for lay in project.effects.layers if lay.type == "spotlight"}

    def _page_range(self, lay):
        lo, hi = sorted((int(lay.get("from_measure")) - 1, int(lay.get("to_measure")) - 1))
        infos = [m for m in self.scene.score.measure_infos if lo <= m.index <= hi and m.rect[2] > 0]
        if not infos:
            return None
        return (min(m.rect[0] for m in infos), min(m.rect[1] for m in infos),
                max(m.rect[0] + m.rect[2] for m in infos), max(m.rect[1] + m.rect[3] for m in infos))

    def _spot_pixels(self, pose):
        from .effects import view_matrix
        M = view_matrix(pose, self.W, self.H)
        out = {}
        for lid, r in self._spots.items():
            if r is not None:
                xs = [M[0, 0] * x + M[0, 1] * y + M[0, 2] for x in (r[0], r[2]) for y in (r[1], r[3])]
                out[lid] = (min(xs), max(xs))
        return out

    def frame(self, k: int) -> np.ndarray:
        t = k / self.fps
        self.scene.apply_time(t)
        pose = self.comp.camera_pose(k, self.project.camera_pose(t))
        self.image.fill(Qt.transparent)
        saved = self.scene.backgroundBrush()
        self.scene.setBackgroundBrush(QBrush(Qt.NoBrush))
        self.scene.rendering = True
        try:
            _paint_scene(self.scene, self.image, pose)
        finally:
            self.scene.rendering = False
            self.scene.setBackgroundBrush(saved)
        bpl = self.image.bytesPerLine()
        ink = np.frombuffer(self.image.constBits(), np.uint8).reshape(self.H, bpl)[:, :self.W * 4]
        alpha = np.ascontiguousarray(ink.reshape(self.H, self.W, 4)[..., 3])
        return self.comp.frame(k, alpha, pose, self._spot_pixels(pose))

    def close(self):
        self.comp.close()


# ====================================================================================== ffmpeg
def _ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _encoder(w, h, fps, out, crf, pix_in="rgba", audio=None, threads=None, preset="medium"):
    cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", pix_in, "-s", f"{w}x{h}",
           "-r", str(fps), "-i", "-"]
    if audio:
        cmd += ["-i", audio]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    if threads:
        cmd += ["-threads", str(threads)]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += ["-movflags", "+faststart", str(out)]
    return cmd


def render_video(scene: SheetScene, project: Project, out_path: str, audio_path: str | None,
                 progress=lambda done, total: True, size: tuple | None = None) -> None:
    """Writes `out_path` (.mp4).  `progress(done, total)` may return False to cancel.  (Without effects;
    see `render_video_parallel` for the produced look.)"""
    s = project.settings
    w, h = size or (s.width, s.height)
    w, h = w - w % 2, h - h % 2  # yuv420p needs even dimensions
    if not project.has_keys():
        raise ValueError("Add at least one camera keyframe first.")
    total = math.ceil(total_duration(scene, project) * s.fps)
    cmd = _encoder(w, h, s.fps, out_path, s.crf, audio=audio_path, preset=s.preset)

    saved_time, saved_sel = scene.time, scene.selectedItems()
    scene.clearSelection()
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log, creationflags=_NO_WINDOW)
    image = QImage(w, h, QImage.Format_RGBA8888)
    cancelled = False
    try:
        for i in range(total):
            render_frame(scene, project, i / s.fps, image)
            proc.stdin.write(memoryview(image.constBits())[: w * h * 4])
            if i % 3 == 0 and progress(i, total) is False:
                cancelled = True
                break
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        code = proc.wait()
        scene.apply_time(saved_time)
        for it in saved_sel:
            it.setSelected(True)
    if cancelled:
        Path(out_path).unlink(missing_ok=True)
        return
    if code != 0:
        log.seek(0)
        raise RuntimeError("ffmpeg failed:\n" + log.read().decode(errors="replace")[-1500:])
    progress(total, total)


# ====================================================================================== parallel effects render
def _worker(job: dict, q) -> None:
    """One process of a parallel render: rebuilds the scene and renders frames k0..k1 into a segment."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication(["render"])   # noqa: F841 - must stay alive
        import cv2
        cv2.setNumThreads(int(job.get("cv_threads", 1)))   # the processes are the parallelism; do not oversubscribe
        from .build import build_score
        project = Project()
        project.load_dict(json.loads(job["project"]))
        score = build_score(project)
        scene = SheetScene(score, project)
        scene.set_cache(False)
        scene.clearSelection()
        loud = effect_loudness(project, job["loudness"])
        r = EffectsRenderer(scene, project, job["w"], job["h"], job["fps"], job["duration"], loud)
        proc = subprocess.Popen(_encoder(job["w"], job["h"], job["fps"], job["out"], job["crf"], "rgb24", None, 2, job["preset"]),
                                stdin=subprocess.PIPE, creationflags=_NO_WINDOW)
        k0, k1 = job["k0"], job["k1"]
        for k in range(k0, k1):
            proc.stdin.write(r.frame(k).tobytes())
            if (k - k0) % 10 == 9 or k == k1 - 1:
                q.put(("progress", job["id"], k - k0 + 1))
        proc.stdin.close()
        code = proc.wait()
        r.close()
        q.put(("done", job["id"], code))
    except BaseException as e:   # report anything, the parent decides
        import traceback
        q.put(("error", job["id"], f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"))


def render_video_parallel(project: Project, out_path: str, audio_path: str | None, loudness_path: str | None,
                          duration: float, progress=lambda done, total: True, workers: int | None = None,
                          size: tuple | None = None, start: float = 0.0, end: float | None = None,
                          cv_threads: int = 1) -> None:
    """Renders the project with effects using several processes (one per slice of the video) and joins the
    slices.  `progress(done, total)` may return False to cancel."""
    s = project.settings
    w, h = size or (s.width, s.height)
    w, h = w - w % 2, h - h % 2
    if not project.has_keys():
        raise ValueError("Add at least one camera keyframe first.")
    fps = s.fps
    n_all = math.ceil(duration * fps)
    k_start = max(int(round(start * fps)), 0)
    k_end = min(int(round(end * fps)) if end else n_all, n_all)
    if k_end <= k_start:
        raise ValueError("The part to render is empty.")
    n = max(1, min(workers or default_workers(), 16, k_end - k_start))
    bounds = np.linspace(k_start, k_end, n + 1).astype(int)
    tmp = Path(tempfile.mkdtemp(prefix="sma_render_"))
    pjson = json.dumps(project.to_dict())
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    jobs = [dict(id=i, cv_threads=cv_threads, project=pjson, loudness=loudness_path, w=w, h=h, fps=fps, duration=duration, crf=s.crf, preset=s.preset,
                 k0=int(bounds[i]), k1=int(bounds[i + 1]), out=str(tmp / f"seg_{i:03d}.mp4"))
            for i in range(n) if bounds[i + 1] > bounds[i]]
    procs = [ctx.Process(target=_worker, args=(j, q), daemon=True) for j in jobs]
    done = {j["id"]: 0 for j in jobs}
    finished, error, cancelled = set(), None, False
    total = k_end - k_start
    for p in procs:
        p.start()
    try:
        while len(finished) < len(jobs) and error is None:
            try:
                kind, jid, val = q.get(timeout=0.2)
                if kind == "progress":
                    done[jid] = val
                elif kind == "done":
                    finished.add(jid)
                    if val != 0:
                        error = f"ffmpeg exited with code {val} in worker {jid}"
                else:
                    error = val
            except queue.Empty:
                if all(not p.is_alive() for p in procs) and len(finished) < len(jobs) and q.empty():
                    error = "A render process stopped unexpectedly."
            if progress(sum(done.values()), total) is False:
                cancelled = True
                break
    finally:
        if cancelled or error:
            for p in procs:
                if p.is_alive():
                    p.terminate()
        for p in procs:
            p.join(timeout=5)
    if cancelled:
        _cleanup(tmp)
        return
    if error:
        _cleanup(tmp)
        raise RuntimeError(error)
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{Path(j['out']).name}'\n" for j in jobs), encoding="utf8")
    cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst)]
    if audio_path:
        cmd += ["-ss", f"{k_start / fps:.4f}", "-t", f"{(k_end - k_start) / fps:.4f}", "-i", audio_path,
                "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-shortest"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", str(out_path)]
    res = subprocess.run(cmd, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    _cleanup(tmp)
    if res.returncode != 0:
        raise RuntimeError("Joining the segments failed:\n" + res.stderr.decode(errors="replace")[-1500:])
    progress(total, total)


def _cleanup(folder: Path):
    for f in folder.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass
