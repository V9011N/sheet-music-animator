# Sheet Music Animator

Load a MusicXML file, get the whole score as empty staves, and have the notes appear as they are
played. Move a resizable **camera window** over the sheet, keyframe it on a **timeline**, and
**render** what the camera sees to an MP4 (with audio).

## Run

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py                      # then click "Open MusicXML…" in the toolbar
.venv\Scripts\python main.py piece.mxl            # or open a file directly
```

## How to use it

| What | How |
|---|---|
| Load a score | **Open MusicXML…** (`.mxl`, `.musicxml`, `.xml`). Notes, rests, beams, ties, dynamics, … appear at the time they are heard in the file (tempo included). |
| Play / pause / scrub | `Space`, click or drag in the timeline, `←/→` (0.1 s), `Shift+←/→` (1 s), `Home`. Ctrl+wheel zooms the timeline, middle-drag pans it. |
| Camera | Drag the orange window in the editor to move it, drag a corner to resize (aspect ratio is locked to the output size). Any change at the playhead creates or updates a keyframe. |
| Keyframes | `K` or double-click the Camera lane to add one; drag a diamond to retime it; right-click for easing (smooth / linear / hold) or delete; `Delete` removes the selected one. |
| Auto camera | **Follow music** (Camera tab) builds a camera path that tracks the music and glides from system to system. "Follow-music width" sets the zoom. |
| Look & timing | Note reveal (fade in or appear instantly; beams, 8va lines, hairpins and "cresc. - - -" lines grow note by note, slurs appear whole when instant), fade-in length, a faint "ghost" of unplayed notes (only a few measures ahead of the playhead, so long pieces stay fast), global note shift (for audio sync), page layout (stacked systems or one long line), ink/paper colours. |
| Fix one element's timing | Click any engraved element (note, rest, beam, slur, arpeggio, clef, barline, …) or drag a box around several, then set *Reveal earlier / later* on the Selection tab. Clefs, key/time signatures, barlines, brackets and measure numbers at the start of a staff are always visible until you tick *Reveal with the music* or give them a time; clef changes inside the music appear with the note that follows them. |
| Output | Resolution, frame rate, audio (built-in piano synth, your own audio file, or none) → **Render video…** or **Save current frame as PNG…**. |
| Projects | `Ctrl+S` saves a `.smanim` file (camera keys, timing tweaks, settings). |

Staves that MuseScore hid because they are empty (`print-object="no"` in the exported MusicXML) stay hidden for those systems; Verovio ignores this itself, so the animator removes them.

## How it works

* `engraver.py` – Verovio engraves the MusicXML to SVG and gives a timemap. The SVG is split into a
  static layer per measure (staff lines, clefs, key/time signatures, barlines) and one tiny SVG per timed
  element. Beams, ties, ledger lines, dynamics, pedal marks… get their time from where they sit
  next to the notes.
* `scene.py` – a Qt graphics scene made of those layers; the editor, the camera preview and the
  exporter all draw this same scene, so the preview is exactly what is rendered.
* `export.py` – draws the camera rectangle for every frame and pipes raw frames to ffmpeg
  (bundled through `imageio-ffmpeg`). A 2-minute piece at 1080p/30 renders in about half a minute.
* `audio.py` – a small additive piano synth driven by the notes in the file, so there is sound
  without a soundfont. Supply your own recording in the Output tab for anything better.

## Known limits

* Notes are engraved by Verovio, so the layout is Verovio's, not your notation program's.
* Only the first page is used; Verovio is asked for one tall page, which is plenty for typical pieces.
* Clefs/key/time signatures at the start of a staff, barlines and the like are always visible unless you time them in the Selection tab.
