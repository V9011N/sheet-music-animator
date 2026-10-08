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
| Fit to a recording | Output tab → **Fit the score to a recording…**. The app listens to the recording (any audio file) and moves every note of the score to where it is played, then uses the recording as the soundtrack. Keyframes already placed move along with the music. **Use the score's own timing** goes back. |
| Produced look | **Effects** tab → *Produced look*: an animated backdrop (sky gradient, drifting mist, wind-blown snow, dark corners), notes that light up when they sound (right hand cyan, left hand amber, with a bloom), camera shake / zoom punch on loud dynamics and accents, a title card, side fades, a spotlight on chosen measures and a fade to black. The **mood** (calm → storm), **hush** and **snow lift** automation lanes appear in the timeline while it is on. *Show the effects in the camera view* previews it. |
| Presets | Effects tab → **Apply preset**. *Winter Wind (storm)* sets all of the above, and the camera, from the measure numbers of the score (align the score to its recording first). *Plain (white page, no effects)* goes back to the blank slate: dark ink on white, no effects, 1080p30, the automatic camera (the score's layout, fitted recording and audio are kept). |
| Output | Resolution, frame rate, audio (built-in piano synth, your own audio file, or none) → **Render video…** or **Save current frame as PNG…**. With the produced look on, a render uses one process per slice of the video (set how many in the Output tab; you can render just a part to try things out). |
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
* `analysis.py` – loudness of a recording, and the alignment of the score to it: semitone-resolved
  features of the recording and of the score (with per-pitch onset detectors) are matched by dynamic time
  warping, coarse then fine, and every note onset is then snapped to the strongest attack of its own
  pitches (Viterbi over candidate attacks).  Checked against the note timing the first Winter Wind
  renderer had worked out for Kissin's recording (with its note list standing in for the score), 80% of
  the notes agree within 30 ms and 96% within 120 ms.
* `effects.py` – `EffectTracks` (everything that varies with time: wind from loudness and note density,
  shake/punch/flash from dynamics and accents, mood, vignette, fades) and `Compositor` (backdrop, snow,
  the score's ink as an alpha mask drawn by Qt, per-note light-up with bloom, spotlight, title).
  `effects_ui.py` is its tab; `presets.py` holds the looks.
* `export.py` – besides the plain renderer, `EffectsRenderer` makes one finished frame and
  `render_video_parallel` renders slices of the video in separate processes and joins them.
* `cli.py` – the same pipeline without a window (see below).
* `audio.py` – a small additive piano synth driven by the notes in the file, so there is sound
  without a soundfont. Supply your own recording in the Output tab for anything better.

## Recreating the Winter Wind video

The "Winter Wind" look (calm Lento introduction, storm from bar 5, lit-up notes, shake on the loud chords,
final spotlight on bar 96) is a preset. You need the MusicXML of the piece (96 measures) and the recording.

* In the editor: **Open MusicXML…** (4 measures per line) → Output tab: **Fit the score to a recording…** →
  Effects tab: **Apply preset** → look at it with *Show the effects in the camera view* → **Render video…**.
* Or in one command: see below. Apply the preset *after* fitting; it places its cues by measure number.

## Rendering without the editor

```
python -m sheet_music_animator.cli render "Winter Wind.mxl" --audio recording.mp3 --align --preset winter_wind --out winter_wind.mp4
```

`--align` fits the score to the recording, `--preset` applies a look, `--start/--end` render only a part,
`--workers N` sets the number of processes, `--save-project x.smanim` keeps the result for the editor.
An existing `.smanim` project can be rendered the same way.

## Known limits

* Notes are engraved by Verovio, so the layout is Verovio's, not your notation program's.
* Only the first page is used; the lines are stacked on one tall page (or laid out on a single line).
* Clefs/key/time signatures at the start of a staff, barlines and the like are always visible unless you time them in the Selection tab.
* Fitting a score to a recording works best for piano-like music; very free rubato or a recording that
  differs from the score (cuts, repeats taken differently) can leave stretches off by a few tenths of a
  second.  Check the timeline density and the lit-up notes, and nudge single notes in the Selection tab.
* Rendering with the produced look takes time: about 0.05 s of compositing per 1080p frame, and the
  processes share the machine's memory bandwidth, so a render tops out around 35-40 frames per second
  however many cores there are (a 3.5-minute 60 fps video: roughly 6 minutes; 1080p30 or 720p60 is
  2-4 times quicker). Try things out with *Render only from … to …* first.
