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
| Playback speed | The **Speed** box in the toolbar plays at 0.1x to 5.0x (the audio follows); **1x** resets it. |
| Tap to Keyframe | After fitting a recording, the toolbar's **Tap to Keyframe** opens a short explanation and asks whether each tap advances by *Notes only* or *Notes and Rests*; **Start** counts down 3 s and plays from the seeker. Press **Space** on every note — each tap sets when the next engraving appears (Space does not pause) — and **Esc** ends the mode. One undo step reverts everything tapped. |
| Play / pause / scrub | `Space`, click or drag in the timeline, `←/→` (0.1 s), `Shift+←/→` (1 s), `Home`. Ctrl+wheel zooms the timeline, middle-drag pans it. |
| Camera | Drag the orange window in the editor to move it, drag a corner to resize (aspect ratio is locked to the output size), drag the round handle above it to rotate. Only the channel you change (x, y, frame size, rotation) gets a keyframe: moving the camera sideways keys x alone, moving it up or down keys y alone. Switch **Add keyframes automatically** off in the Camera tab to edit the camera at the playhead without creating keys. |
| Keyframes | Each channel has its own lane; the **Camera** label of the timeline is a drop-down that shows or hides the lanes. `K` or double-click a lane adds a key; drag a diamond to retime it; `Ctrl+click` toggles a key, `Shift+click` selects a range, `Ctrl+A` selects all; dragging moves every selected key. Right-click for easing (smooth / linear / hold) or delete; `Delete` removes the selection. |
| Auto camera | **Follow music** (Camera tab) builds a camera path that tracks the music and glides from line to line. It lays down the **x** channel (and the frame size); **y** only gets a key where the camera arrives at a line and where it leaves it, and once you have keyed y yourself, following the music (again) leaves it alone. "Follow-music width" sets the zoom, and "Follow-music lead (+) / lag (−)" how far the camera sits ahead of (or behind) the notes being played, as a share of the frame (0 % centres them; the default +25 % shows what is coming). While the camera path is still the automatic one it follows that setting live. |
| Undo / redo | Arrow buttons in the toolbar, `Ctrl+Z` / `Ctrl+Y` (`Ctrl+Shift+Z`). Closing with unsaved changes asks whether to save. |
| Look & timing | Font of all text (default Times New Roman), note reveal (appear instantly or fade in; beams, 8va lines and hairpins grow note by note), a faint "ghost" of unplayed notes, global note shift, measures per line, ink/paper colours. |
| Select | Click any engraved element, or click the white space of a measure (`Shift+click` for a range, `Ctrl+click` to add). |
| Fix one element | With elements selected, the Selection tab sets *Reveal earlier / later*. Everything except noteheads, stems/flags and beams can be **dragged** to move it, **stretched** with the white handles (edges stretch one direction, corners both), **rotated** with the round handle above it (Shift snaps to 15°) and **deleted** (`Delete` key or the Selection tab; *Restore deleted engravings* brings them back). *Reset position and size* undoes the geometry. Clefs, key/time signatures, barlines… at the start of a line are always visible unless you tick *Reveal with the music* or give them a time. |
| Hide things in measures | With measures selected, the Selection tab has a drop-down of categories (fingerings, tuplet numbers, articulations, dynamics, slurs, …) that can be switched off for those measures. |
| Change the line breaks | Select the first measure of a line and press *Move this line up*: the whole line joins the previous one. Select a measure in the middle of a line and press *Move from here to the next line*: it and the measures after it on that line move to the start of the next line (a new line is made after the last one) and get their own clefs, key signature and bracket. The canvas (width and height) and the camera keys follow. |
| Fit to a recording | The bold **Fit Score to Recording…** button in the toolbar (right after *Show camera*). The app listens to the recording (any audio file) and moves every note of the score to where it is played, then uses the recording as the soundtrack. Keyframes already placed move along with the music. A window then reports *Audio synced with xx% confidence!* — *Few adjustments need to be made* above 95%, *Some* from 85% to 95%, *Many* below 85% — and **Sync Heat Map** (toolbar) colours the score green where the sync is sure of itself and red where it is not. **Use the Score's Own Timing** goes back. |
| Looks and layers | **Effects** tab. A look is a stack of **layers** drawn bottom to top: backdrops (colour, gradient, flowing mist/fire/water texture, your own photo or video), particles (snow, embers, fireflies, rain, bubbles, or sparks that fly off every note), the notation itself (ink colour, glow, side fades), note highlights (glow, ripple, star flare, column of light; coloured by hand, pitch or register), spotlights, finishing (bloom, dark corners, colour grade, blur, colour fringing, film grain), text, and camera effects (shake, zoom, sway). Add, remove, duplicate, reorder and switch layers off; every layer has its own blend mode, opacity and timing. See *Making your own look* below. |
| Following the music | Any number setting has a **Link** button: make it follow the loudness, how many notes are playing, the *events* (loud dynamics and accents) or every single note, the pitch height, or one of your own **automation lanes**. Several links can add up. This is what makes snow blow harder in loud passages, the camera shake on a fortissimo, or the sky change colour at a cue. |
| Automation lanes | Add any number of named lanes (Effects tab → Automation lanes); they appear in the timeline under the camera lanes and are keyframed like the camera. A lane called *Storm* fading a gradient between two palettes is one link away. |
| Looks you can start from | Effects tab → **Looks**: *Plain*, *Winter Wind (storm)*, *Ember (fire)*, *Fireflies (night garden)*, *Ocean (underwater)*, *Golden rain*, *Neon (night city)*, *Monochrome storm*, *Paper (daylight)* and *Photo backdrop (your own picture)*. **Save this look…** keeps your own as a JSON file (in `~/.sheet_music_animator/looks`; send the file to share it). Times in a look are anchored to measures, so a look made for one piece fits another. |
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
* `layers.py` – the catalogue of layer types: for each, its settings, ranges, defaults and which settings can
  follow a signal. The editor builds its panels from it, so adding a new effect means adding one entry there
  and one drawer in `effects.py`.
* `signals.py` – the signals of the music (loudness, density, events from dynamics/accents/your markers,
  every note, pitch) and of your lanes, and the evaluation of links; also the camera layers' motion.
* `effects.py` – draws a stack of layers into a finished frame: a float canvas with blend modes, one drawer per
  layer type, the notation's ink arriving as an alpha mask from the Qt scene.
* `looks.py` / `stacks.py` – saved stacks: applying one, capturing the current setup as a new one, the built-ins.
  `effects_ui.py` is the tab.
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

## Making your own look

1. Effects tab → tick *Produced look* (it starts with a backdrop, the notation and a note highlight) or apply
   one of the looks and change it.
2. **Add layer ▾** and pick from the categories. The layer list is drawn bottom to top (the top of the list is
   in front); put layers *below* the notation for the backdrop, *above* it for light and overlays.
3. Each layer's settings are listed under it. Colours open a picker, *Appears at* has a ⌖ button that takes the
   playhead time, a *Picture or video* layer takes any photo or clip (it zooms slowly, loops, can be blurred).
4. **Link** a setting to make it move: pick a signal and how much it adds. E.g. on a *Particles* layer link
   *Speed* to *Loudness + density* so the wind blows with the music, link a *Camera shake*'s amount to *Events*,
   link a *Solid colour* layer (blend *add*) to *Big events* for a lightning flash, link a *Gradient*'s mix to a
   lane you named *Storm* and keyframe the lane where the storm breaks.
5. **Save this look…** when you like it.

Recipes: a *fade to black* is a black *Solid colour* with *Appears at* −1.0 (one second before the end) and
*Fades in over* 1.0; a *title* is a *Text* layer with *Appears at* and *Fades out over*; a *vignette* of colour
is the *Dark corners* layer with another colour; a *flash on every note* is a *Solid colour* (add) linked to
*Every note that starts*.

## Recreating the Winter Wind video

The "Winter Wind (storm)" look (calm Lento introduction, storm from measure 5, lit-up notes, shake on the loud
chords, final spotlight on measure 96) is one of the built-in looks. You need the MusicXML of the piece
(96 measures) and the recording.

* In the editor: **Open MusicXML…** (4 measures per line) → toolbar: **Fit Score to Recording…** →
  Effects tab: Looks → *Winter Wind (storm)* → **Apply** → look at it with *Show the effects in the camera view*
  → **Render video…**.
* Or in one command: see below. Apply the look *after* fitting; it places its cues by measure number.

## Rendering without the editor

```
python -m sheet_music_animator.cli render "Winter Wind.mxl" --audio recording.mp3 --align --look winter_wind --out winter_wind.mp4
```

`--align` fits the score to the recording, `--look` applies a look (a built-in name such as `ember`, the name of one of yours, or a `.json` file), `--start/--end` render only a part,
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
