# Benchmark: engraving against PDFs, sync against recordings

Uses the pieces in `01_XMLs_PDFs_MP3s_for_training/` (one folder per piece: the MusicXML, the PDF it was
printed from, one or more recordings). Run from the project folder with the project's Python.

| Script | What it does |
|---|---|
| `engrave_compare.py <piece> [out dir] [--pages a-b]` | Engraves the score with the PDF's own line and page breaks (read from the MusicXML's `<print>` marks) and writes one image per PDF page: the PDF on the left, the app's lines on the right (`out/engraving/`). |
| `ms_compare.py <score (.mscz/.mxl)> <pdf> [out dir] [--pages a-b]` | The same with MuseScore engraving: each page MuseScore laid out, drawn through the app's own scene, next to the PDF page (`out/musescore/`). |
| `sync_eval.py align <piece> [recording]` | Fits the score to a recording with the app's `align_score` (cached by tag, `TAG=...`). |
| `sync_eval.py viz <piece> [recording] t0 t1` | Spectrogram of the recording (grey: what sounds, red: what is struck) with the fitted notes on it (boxes, green onsets), measure numbers and the confidence of every onset (`out/viz/`). |
| `sync_eval.py gt` | Scores the fit against note times worked out by hand for one recording (set `SMA_GROUND_TRUTH` to their folder). |
| `exp.py <tag> [CONSTANT=value ...]` | Fits every score to every recording (in parallel), optionally with constants of `analysis.py` changed, and prints the confidence per recording, at the start and the end, and the ground-truth numbers. |

`<piece>` and `[recording]` are parts of the folder and file names (`"TE 4" Cziffra`).

Set `ENGINE=musescore` for `sync_eval.py` / `exp.py` to fit the notes MuseScore plays (a piece folder's `.mscz` is then preferred over its MusicXML). Run one MuseScore job at a time: two at once make one of them fail.
