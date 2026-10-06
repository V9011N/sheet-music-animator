"""Render the camera's view of the scene to a video file via ffmpeg."""
from __future__ import annotations

import math
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from .project import Project
from .scene import SheetScene


def total_duration(scene: SheetScene, project: Project) -> float:
    return scene.score.duration + project.settings.offset + project.settings.tail


def render_frame(scene: SheetScene, project: Project, t: float, image: QImage):
    scene.apply_time(t)
    cam = project.camera_rect(t)
    image.fill(QColor(project.settings.paper))
    p = QPainter(image)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
    scene.render(p, QRectF(0, 0, image.width(), image.height()), QRectF(*cam), Qt.IgnoreAspectRatio)
    p.end()


def render_video(scene: SheetScene, project: Project, out_path: str, audio_path: str | None,
                 progress=lambda done, total: True, size: tuple | None = None) -> None:
    """Writes `out_path` (.mp4).  `progress(done, total)` may return False to cancel."""
    s = project.settings
    w, h = size or (s.width, s.height)
    w, h = w - w % 2, h - h % 2  # yuv420p needs even dimensions
    if not project.keys:
        raise ValueError("Add at least one camera keyframe first.")
    total = math.ceil(total_duration(scene, project) * s.fps)
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", str(s.fps), "-i", "-"]
    if audio_path:
        cmd += ["-i", audio_path]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(s.crf), "-pix_fmt", "yuv420p"]
    if audio_path:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += ["-movflags", "+faststart", str(out_path)]

    saved_time, saved_sel = scene.time, scene.selectedItems()
    scene.clearSelection()
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log)
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
