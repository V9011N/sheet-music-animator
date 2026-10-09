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
| Guide | The first time the app opens it offers an interactive guide (skip it with *Skip guide*): it dims the window, spotlights one control at a time and waits for you to try it — open a score (a sample is included), move the camera, play, add and select keyframes, change the look, select measures, hide engravings, change line breaks, move elements, undo, output and save. Replay it any time with **? Guide** in the toolbar. |
| Load a score | **Open MusicXML…** (`.mxl`, `.musicxml`, `.xml`). You are asked how many **measures go on each line** (or put the **whole score on one line**); this decides the canvas size and the automatic camera. Notes, rests, beams, ties, dynamics, … appear at the time they are heard in the file (tempo included), instantly by default. |
| Play / pause / scrub | `Space`, click or drag in the timeline, `←/→` (0.1 s), `Shift+←/→` (1 s), `Home`. Ctrl+wheel zooms the timeline, middle-drag pans it. |
| Camera | Drag the orange window in the editor to move it, drag a corner to resize (aspect ratio is locked to the output size), drag the round handle above it to rotate. Only the channel you change (position, frame size, rotation) gets a keyframe. Switch **Add keyframes automatically** off in the Camera tab to edit the camera at the playhead without creating keys. |
| Keyframes | Each channel has its own lane; the **Camera** label of the timeline is a drop-down that shows or hides the lanes. `K` or double-click a lane adds a key; drag a diamond to retime it; `Ctrl+click` toggles a key, `Shift+click` selects a range, `Ctrl+A` selects all; dragging moves every selected key. Right-click for easing (smooth / linear / hold) or delete; `Delete` removes the selection. |
| Auto camera | **Follow music** (Camera tab) builds a camera path that tracks the music and glides from line to line. "Follow-music width" sets the zoom. |
| Undo / redo | Arrow buttons in the toolbar, `Ctrl+Z` / `Ctrl+Y` (`Ctrl+Shift+Z`). Closing with unsaved changes asks whether to save. |
| Look & timing | Font of all text (default Times New Roman), note reveal (appear instantly or fade in; beams, 8va lines and hairpins grow note by note), a faint "ghost" of unplayed notes, global note shift, measures per line, ink/paper colours. |
| Select | Click any engraved element, or click the white space of a measure (`Shift+click` for a range, `Ctrl+click` to add). |
| Fix one element | With elements selected, the Selection tab sets *Reveal earlier / later*. Everything except noteheads, stems/flags and beams can be **dragged** to move it and resized by dragging a corner handle (*Reset position* undoes it). Clefs, key/time signatures, barlines… at the start of a staff are always visible until you tick *Reveal with the music* or give them a time. |
| Hide things in measures | With measures selected, the Selection tab has a drop-down of categories (fingerings, tuplet numbers, articulations, dynamics, slurs, …) that can be switched off for those measures. |
| Change the line breaks | Select the first measure of a line and press *Move this line up*: the whole line joins the previous one. Select a measure in the middle of a line and press *Move from here to the next line*: it and the measures after it on that line move to the start of the next line (a new line is made after the last one) and get their own clefs, key signature and bracket. The canvas (width and height) and the camera keys follow. |
| Output | Resolution, frame rate, audio (built-in piano synth, your own audio file, or none) → **Render video…** or **Save current frame as PNG…**. |
| Projects | `Ctrl+S` saves a `.smanim` file (camera keys, timing tweaks, hidden categories, moved elements, line breaks, settings). |

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
* Only the first page is used; the lines are stacked on one tall page (or laid out on a single line).
* Clefs/key/time signatures at the start of a staff, barlines and the like are always visible unless you time them in the Selection tab.
