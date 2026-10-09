"""Audio analysis: loudness of a recording and alignment of the score to it.

`align_score` fits the (mechanical) timing of the MusicXML to a real performance: it compares
pitch-class features of the recording with features synthesised from the score (dynamic time warping,
coarse to fine) and then snaps every note onset to the nearest attack of that note's pitch.
The result is a monotone time map (score seconds -> recording seconds) that `Score.warp` applies.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

SR = 22050
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ------------------------------------------------------------------------------------ loading
def decode_audio(path: str, sr: int = SR) -> np.ndarray:
    """Mono float32 samples of any audio/video file (decoded by the bundled ffmpeg)."""
    proc = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(path), "-vn",
                           "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not proc.stdout:
        raise ValueError("Could not read audio from %s\n%s" % (path, proc.stderr.decode(errors="replace")[-400:]))
    return np.frombuffer(proc.stdout, np.float32)


def _cache_path(path: str, tag: str) -> Path:
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|{tag}".encode()).hexdigest()[:16]
    d = Path(tempfile.gettempdir()) / "sheet_music_animator"
    d.mkdir(exist_ok=True)
    return d / f"{key}.npy"


def loudness(path: str, fps: int = 100) -> tuple[np.ndarray, int]:
    """RMS amplitude of the recording, `fps` values per second (cached; first call decodes the file)."""
    cache = _cache_path(path, f"rms{fps}")
    if cache.exists():
        return np.load(cache), fps
    y = decode_audio(path)
    hop = SR // fps
    n = len(y) // hop
    rms = np.sqrt(np.mean(y[:n * hop].reshape(n, hop).astype(np.float64) ** 2, axis=1)).astype(np.float32)
    np.save(cache, rms)
    return rms, fps


# ------------------------------------------------------------------------------------ features
def stft_mag(y: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """|STFT| (frames x bins); frame i is centred on sample i*hop."""
    y = np.pad(y, n_fft // 2)
    win = np.hanning(n_fft).astype(np.float32)
    frames = sliding_window_view(y, n_fft)[::hop]
    out = np.empty((len(frames), n_fft // 2 + 1), np.float32)
    for s in range(0, len(frames), 512):
        out[s:s + 512] = np.abs(np.fft.rfft(frames[s:s + 512] * win, axis=1))
    return out


LO_MIDI, HI_MIDI = 33, 105              # pitch range that is compared (A1 .. A7)
NP = HI_MIDI - LO_MIDI + 1
LEVEL_W, FLUX_W = 0.75, 1.0             # weights of "what sounds" and "what just started" in the match


SPLIT_MIDI = 55          # below this the long window is used (bass notes need the frequency resolution)


def _band_matrix(n_fft: int, lo: int, hi: int):
    """Maps spectrum bins to semitone bins (columns of the full LO_MIDI..HI_MIDI range); only pitches lo..hi."""
    f = np.fft.rfftfreq(n_fft, 1 / SR)
    sel = np.nonzero((f >= 40.0) & (f <= 3800.0))[0]
    midi = np.round(69 + 12 * np.log2(f[sel] / 440.0)).astype(int)
    ok = (midi >= max(lo, LO_MIDI)) & (midi <= min(hi, HI_MIDI))
    M = np.zeros((len(f), NP), np.float32)
    M[sel[ok], midi[ok] - LO_MIDI] = 1.0
    return M


NORM_FLOOR = 0.25      # frames quieter than this fraction of the typical frame are not scaled up to full size
PENALTY = 0.32         # extra cost of advancing only one of the two sequences
DIAG_WEIGHT = 2.0      # a diagonal step counts its cell twice, so every path between two points weighs the same
START_SLACK = 0.0      # seconds the first note may come after the start of the (trimmed) recording
START_PENALTY = 0.3    # cost per second of that delay
OPEN_END = False       # (coarse stage) the whole recording is matched; the fine stage ends openly, inside a corridor
END_COST = 1.0         # (fine stage) cost of every frame of the recording left over after the last note: what
                       # matching it to nothing would cost, so ending early never pays for itself
END_SPAN = 4.0         # seconds at the end of the score whose corridor is widened...
END_CORRIDOR = 4.0     # ...to this many seconds either side: a long held final chord rings on past its written length
CORRIDOR = 1.5         # seconds either side of the coarse path searched by the fine alignment
PULL = 0.05            # (fine stage) cost per frame and second of lying away from the coarse path


def _unit_rows(x: np.ndarray, floor: float = 0.0) -> np.ndarray:
    n = np.linalg.norm(x, axis=1, keepdims=True)
    if floor:
        nz = n[n > 1e-6]
        n = np.maximum(n, floor * (np.median(nz) if len(nz) else 1.0))
    return np.where(n > 1e-6, x / np.maximum(n, 1e-9), 0.0).astype(np.float32)


def _combine(level: np.ndarray, flux: np.ndarray, floor: float = 0.0) -> np.ndarray:
    return np.hstack([LEVEL_W * _unit_rows(level, floor), FLUX_W * _unit_rows(flux, floor)]) / np.hypot(LEVEL_W, FLUX_W)


def audio_features(y: np.ndarray, fps: int) -> np.ndarray:
    """Per-semitone loudness of the recording and how much of it is new (an onset detector per pitch)."""
    n_fft = 4096
    mag = stft_mag(y, n_fft, SR // fps)
    ref = np.percentile(mag[:, 20:], 99.5) + 1e-9
    L = np.log1p(30.0 * mag / ref) @ _band_matrix(n_fft, LO_MIDI, HI_MIDI)
    L = np.maximum(L - np.percentile(L, 20, axis=0), 0)       # remove the steady noise floor of each pitch
    k = max(int(round(0.04 * fps)), 1)
    F = np.zeros_like(L)
    F[k:] = np.maximum(L[k:] - L[:-k], 0)
    return _combine(L, F, NORM_FLOOR)


PARTIALS = ((0, 1.0), (12, 0.6), (19, 0.45), (24, 0.3), (28, 0.2), (31, 0.15))   # semitones above the fundamental


def score_features(notes, fps: int, length: float) -> np.ndarray:
    """What the score should sound like in the same features: every note contributes its partials."""
    n = int(length * fps) + 2
    Lv = np.zeros((n, NP), np.float32)
    Fx = np.zeros((n, NP), np.float32)
    tail = np.exp(-np.arange(1, int(0.4 * fps) + 1) / (0.15 * fps))   # the ring after the key is released
    for pitch, start, end, *_ in notes:
        v = np.zeros(NP, np.float32)
        for off, w in PARTIALS:
            b = pitch + off - LO_MIDI
            if 0 <= b < NP:
                v[b] += w
        a = int(start * fps)
        e = max(int(end * fps), a + 1)
        Lv[a:e + 1] += v
        t = min(e + 1 + len(tail), n)
        Lv[e + 1:t] += tail[:t - e - 1, None] * v * 0.6
        Fx[a] += v
        if a + 1 < n:
            Fx[a + 1] += 0.5 * v
    return _combine(Lv, Fx)


# ------------------------------------------------------------------------------------ DTW
def _dtw(X, Y, lo, hi, penalty=None, fps=50, open_end=None, centre=None, pull=0.0):
    """Dynamic time warping of rows of X against rows of Y inside the window [lo[i], hi[i]) of every row.
    Cost is cosine distance; stepping along only one sequence costs `penalty` extra.  A diagonal step counts
    its cell DIAG_WEIGHT times (the symmetric form): otherwise a path that takes more steps -- one that lets a
    passage last longer or shorter in the recording than in the score -- collects more cells and pays the
    typical mismatch of a cell (about 0.6 in dense, pedalled music) on top of `penalty` for every step it
    deviates, which squeezes slow passages and drags fast ones.  With `centre` (a column for every row) each
    cell also costs `pull` per second it lies away from it.  Returns, for every row of X, the (float) column it
    is matched to."""
    penalty = PENALTY if penalty is None else penalty
    open_end = OPEN_END if open_end is None else open_end
    n, m = len(X), len(Y)
    Ds = []
    prev, plo = None, 0
    for i in range(n):
        a, b = int(lo[i]), int(hi[i])
        c = (1.0 - Y[a:b] @ X[i]).astype(np.float64)
        if pull:
            c += pull * np.abs(np.arange(a, b) - centre[i]) / fps
        if prev is None:
            q = np.full(b - a, np.inf)
            if a == 0:                                         # the first note may sound a little after the recording starts
                late = np.arange(0, min(b, int(START_SLACK * fps) + 1))
                q[late] = c[late] + START_PENALTY * late / fps
        else:
            ext = np.full(m + 2, np.inf)                       # previous row, indexed by column + 1
            ext[plo + 1:plo + 1 + len(prev)] = prev
            j = np.arange(a, b)
            q = np.minimum(c + ext[j + 1] + penalty, DIAG_WEIGHT * c + ext[j])   # from above (+penalty) or diagonally
        T = np.cumsum(c + penalty)
        D = np.minimum.accumulate(q - T) + T                   # horizontal steps within the row
        Ds.append(D.astype(np.float32))
        prev, plo = D, a
    # backtrace from the best end point (open end: the recording may continue after the last note)
    i = n - 1
    a = int(lo[i])
    tail = np.maximum(m - 1 - np.arange(a, int(hi[i])), 0)
    j = a + int(np.argmin(Ds[i] + (END_COST * tail if open_end else np.where(tail > 0, np.inf, 0))))
    cols = [[] for _ in range(n)]
    cols[i].append(j)
    pen = np.float32(penalty)
    while i > 0 or j > 0:
        a = int(lo[i])
        best, step = np.inf, None
        if i > 0:
            pa = int(lo[i - 1])
            Dp = Ds[i - 1]
            if j - 1 >= pa and j - 1 - pa < len(Dp) and j - 1 >= 0:
                cell = 1.0 - float(Y[j] @ X[i]) + (pull * abs(j - centre[i]) / fps if pull else 0.0)
                extra = (DIAG_WEIGHT - 1.0) * cell                 # the diagonal step's cell counts again
                best, step = Dp[j - 1 - pa] + extra, (-1, -1)
            if pa <= j < pa + len(Dp) and Dp[j - pa] + pen < best:
                best, step = Dp[j - pa] + pen, (-1, 0)
        if j > a and Ds[i][j - 1 - a] + pen < best:
            best, step = Ds[i][j - 1 - a] + pen, (0, -1)
        if step is None:
            break
        i, j = i + step[0], j + step[1]
        cols[i].append(j)
    return np.array([np.mean(c) if c else np.nan for c in cols])


def _fill(a):
    idx = np.arange(len(a))
    good = ~np.isnan(a)
    return np.interp(idx, idx[good], a[good])


@dataclass
class Alignment:
    nominal: np.ndarray       # score seconds (anchor points)
    actual: np.ndarray        # matching seconds in the recording
    shifts: np.ndarray        # how far the refinement moved each onset from the DTW estimate (s)
    confidence: np.ndarray | None = None   # 0..1 per anchor point: how sure the algorithm is about it
    evidence: dict | None = None           # the separate measures the confidence is made of (per anchor)

    def points(self) -> list:
        return [[round(float(a), 4), round(float(b), 4)] for a, b in zip(self.nominal, self.actual)]

    @property
    def overall(self) -> float:
        """The headline confidence, 0..1 (see `overall_confidence`)."""
        return overall_confidence(self.confidence)

    def heat(self) -> list:
        """[[recording seconds, confidence 0..1], ...] for the heat map over the score."""
        if self.confidence is None:
            return []
        return [[round(float(a), 4), round(float(c), 4)] for a, c in zip(self.actual, self.confidence)]


SILENCE = 0.02       # frames quieter than this fraction of the loudest one count as silence at the ends


def _active_range(y: np.ndarray, fps: int = 50, floor: float | None = None):
    floor = SILENCE if floor is None else floor
    hop = SR // fps
    n = len(y) // hop
    rms = np.sqrt(np.mean(y[:n * hop].reshape(n, hop) ** 2, axis=1))
    loud = np.nonzero(rms > floor * rms.max())[0]
    return (loud[0] / fps, (loud[-1] + 1) / fps) if len(loud) else (0.0, len(y) / SR)


def align_score(notes, audio_path: str, progress=None, refine: bool = True) -> Alignment:
    """Time map from score time to the recording.  `notes`: (midi pitch, start, end, ...) in score seconds."""
    say = progress or (lambda f, s="": True)
    notes = sorted(notes, key=lambda n: n[1])
    if len(notes) < 8:
        raise ValueError("The score has too few notes to align.")
    say(0.02, "Reading the recording…")
    y = decode_audio(audio_path)
    a0, a1 = _active_range(y)
    s0, s1 = notes[0][1], max(n[2] for n in notes)
    if a1 - a0 < 0.5 * (s1 - s0) * 0.25:
        raise ValueError("The recording is much shorter than the score.")
    ya = y[int(a0 * SR):int(a1 * SR)]
    shifted = [(p, s - s0, e - s0) for p, s, e, *_ in notes]
    ls = s1 - s0

    # coarse: the whole matrix at 10 frames/s
    say(0.08, "Coarse alignment…")
    fc = 10
    Xc, Yc = score_features(shifted, fc, ls), audio_features(ya, fc)
    nxc, nyc = len(Xc), len(Yc)
    wc = _dtw(Xc, Yc, np.zeros(nxc, int), np.full(nxc, nyc), fps=fc)
    wc = np.maximum.accumulate(_fill(wc))

    # fine: 50 frames/s inside a corridor around the coarse path
    say(0.25, "Fine alignment…")
    ff = 50
    Xf, Yf = score_features(shifted, ff, ls), audio_features(ya, ff)
    nx, ny = len(Xf), len(Yf)
    centre = np.interp(np.arange(nx) / ff, np.arange(nxc) / fc, wc / fc) * ff
    r = np.full(nx, int(CORRIDOR * ff))
    r[max(nx - int(END_SPAN * ff), 0):] = int(END_CORRIDOR * ff)   # the last seconds: the final chord may ring on
    lo = np.maximum.accumulate(np.clip(np.round(centre - r), 0, ny - 1)).astype(int)
    hi = np.maximum.accumulate(np.clip(np.round(centre + r) + 1, 1, ny)).astype(int)
    lo[0] = 0
    wf = _fill(_dtw(Xf, Yf, lo, hi, fps=ff, open_end=True, centre=centre, pull=PULL))
    wf = np.maximum.accumulate(wf) / ff                                  # recording time of each score frame

    onsets = np.unique(np.round([n[1] - s0 for n in notes], 4))
    est = np.interp(onsets, np.arange(nx) / ff, wf)
    shifts = np.zeros(len(onsets))
    strength = clarity = np.zeros(len(onsets))
    est_dtw = est.copy()
    if refine:
        say(0.7, "Snapping notes to their attacks…")
        est2, strength, clarity = _refine(ya, shifted, onsets, est)
        shifts = est2 - est
        est = est2
    est = np.maximum.accumulate(est)
    est += np.arange(len(est)) * 1e-5                                    # strictly increasing
    say(0.9, "Judging the fit…")
    conf, evidence = onset_confidence(Xf, Yf, wf, onsets, est, shifts, strength, clarity, ff)
    say(1.0, "Done")
    return Alignment(onsets + s0, est + a0, shifts, conf, evidence)


# ------------------------------------------------------------------------------------ confidence
ATTACK_FLOOR, ATTACK_FULL = 1.5, 2.6        # attack specificity (onset energy in the note's bins vs the average bin) that counts as unmistakable
CLEAR_FLOOR, CLEAR_FULL = 1.8, 4.0   # broadband attack clarity (z-score against the surrounding seconds)
CLEAR_NEEDS_MATCH = 0.6      # match (0..1) at which a clear attack counts in full
MATCH_FULL = 0.13        # how far the fit's similarity must stand above that of unrelated moments to be fully convincing
MATCH_DECOYS = (-4.0, -2.5, -1.2, 1.2, 2.5, 4.0)   # seconds the recording is shifted by for the comparison
SIM_WINDOW = 0.15        # seconds after the onset over which the match is averaged
STEADY_FREE = 0.9        # a note's tempo may differ from its neighbours' by e^0.9 = 2.5x at no cost
STEADY_SPAN = 1.0
SNAP_FREE = 0.10         # snapping an onset by less than this (s) says nothing against it
SNAP_SPAN = 0.30
CONF_WEIGHTS = {"attack": 0.40, "match": 0.35, "steady": 0.15, "snap": 0.10}


def onset_confidence(Xf, Yf, wf, onsets, est, shifts, strength, clarity, ff):
    """How sure the alignment is about every onset (0..1), from four independent kinds of evidence:

    * attack  - did an attack turn up where the note was placed: one specific to the note's own pitches, or
                simply an unmistakable event (big chords after a rest)?
    * match   - does the recording sound like the score around that moment (pitch content, along the path)?
    * steady  - is the local tempo believable compared with the notes around it (no sudden lurches)?
    * snap    - did the attack search agree with the coarse alignment, or did it have to drag the note far?
    Also returns the four components."""
    n = len(onsets)
    ny = len(Yf)
    cols = np.clip(np.round(wf * ff).astype(int), 0, ny - 1)
    has = np.linalg.norm(Xf, axis=1) > 1e-6           # frames where the score has something to say

    def frame_sim(c):
        out = np.full(len(Xf), -1.0)
        for d in (-2, -1, 0, 1, 2):                   # a couple of frames of slack either way
            out = np.maximum(out, np.einsum("ij,ij->i", Yf[np.clip(c + d, 0, ny - 1)], Xf))
        return out

    sim = frame_sim(cols)
    # what the same score frames would match if the recording were somewhere else: the typical similarity
    # of unrelated moments.  A fit is convincing when it stands out from that.
    base = np.mean([frame_sim(np.clip(cols + int(round(o * ff)), 0, ny - 1)) for o in MATCH_DECOYS], axis=0)
    w = max(int(SIM_WINDOW * ff), 1)
    match = np.zeros(n)
    sim_raw = np.zeros(n)
    for k, t in enumerate(onsets):
        i = int(round(t * ff))
        m = has[i:i + w]
        if m.any():
            match[k] = float(np.mean((sim - base)[i:i + w][m]))
            sim_raw[k] = float(np.mean(sim[i:i + w][m]))
    match_c = np.clip(match / MATCH_FULL, 0, 1)
    attack_c = np.clip((strength - ATTACK_FLOOR) / (ATTACK_FULL - ATTACK_FLOOR), 0, 1)
    clear_c = np.clip((clarity - CLEAR_FLOOR) / (CLEAR_FULL - CLEAR_FLOOR), 0, 1)
    # an attack that merely stands out counts only where the recording also sounds like the score there
    attack_c = np.maximum(attack_c, clear_c * np.clip(match_c / CLEAR_NEEDS_MATCH, 0, 1))

    steady_c = np.ones(n)
    if n > 2:
        gaps = np.diff(onsets)
        r = np.diff(est) / np.maximum(gaps, 1e-3)
        lr = np.log(np.clip(r, 1e-3, 1e3))
        med = np.array([np.median(lr[max(0, i - 6):i + 7]) for i in range(len(lr))])
        g = np.clip(1 - np.maximum(np.abs(lr - med) - STEADY_FREE, 0) / STEADY_SPAN, 0, 1)
        steady_c = np.minimum(np.r_[g, g[-1]], np.r_[g[0], g])
    snap_c = np.clip(1 - np.maximum(np.abs(shifts) - SNAP_FREE, 0) / SNAP_SPAN, 0, 1)
    conf = (CONF_WEIGHTS["attack"] * attack_c + CONF_WEIGHTS["match"] * match_c +
            CONF_WEIGHTS["steady"] * steady_c + CONF_WEIGHTS["snap"] * snap_c)
    ev = {"attack": attack_c, "match": match_c, "steady": steady_c, "snap": snap_c, "sim": sim_raw, "margin": match}
    return np.clip(conf, 0, 1), ev


LOST_BELOW = 0.4         # a note this unsure counts as lost
MILD_WEIGHT = 0.4        # how much general, diffuse uncertainty weighs...
LOST_WEIGHT = 1.2        # ...against the share of notes that were lost (a few bad bars matter more)


def overall_confidence(conf) -> float:
    """One number (0..1) for the whole fit.  Real recordings leave a little doubt about many notes (dense
    chords, a pedalled bass), which says little about the result; notes that are plainly lost -- a skipped
    or misread bar -- say a lot.  So: 1 minus a small share of the average doubt, minus the share of lost
    notes times a larger factor.  Calibrated so that a good fit of a real performance with a few trouble
    spots lands around 90 %, and a recording of something else lands near zero."""
    if conf is None or len(conf) == 0:
        return 0.0
    c = np.asarray(conf, float)
    score = 1.0 - MILD_WEIGHT * np.mean(1.0 - c) - LOST_WEIGHT * np.mean(c < LOST_BELOW)
    # when the average itself is poor the recording is probably something else: never let the lenient
    # per-note weighting hide that
    score *= float(np.clip((c.mean() - 0.3) / 0.45, 0.0, 1.0))
    return float(np.clip(score, 0, 1)) + 0.0


SNAP_WINDOW = 0.55       # how far (s) from the DTW estimate an attack may be taken in dense music...
SNAP_SPARSE = 2.5        # ...and this many times the gap to the neighbouring notes in sparse music (a long pause)
SNAP_MAX = 3.0
SNAP_CANDIDATES = 10
SNAP_PEAK = 0.9          # an attack must stand this far (z) above the rest of the window to be a candidate
SNAP_FREE_RATIO = 2.2    # an interval between two notes may differ from the DTW's by this factor at no cost
SNAP_RATIO_COST = 1.5
SNAP_NEAR = 0.2          # notes written closer than this (s) are expected to be played close together
SNAP_EARLY = 0.25        # ...and one that lies more than this ahead of the next one is suspicious
SNAP_TOGETHER = 0.03     # how close to the next note's attack a candidate must be to count as the same moment


def _refine(ya, notes, onsets, est):
    """Choose, for every onset, the attack of its own pitches that makes the best monotone sequence.

    Candidates are the peaks of a per-pitch onset detector around the DTW estimate; a Viterbi pass
    maximises their strength while keeping the order and not changing the spacing of neighbouring
    notes by more than SNAP_FREE_RATIO."""
    hop, n_fft = 221, 2048                              # 10 ms
    mag = stft_mag(ya, n_fft, hop)
    L = np.log1p(30.0 * mag / (np.percentile(mag[:, 10:], 99.5) + 1e-9))
    flux = np.zeros_like(L)
    flux[2:] = np.maximum(L[2:] - np.maximum(L[1:-1], L[:-2]), 0)
    binhz = SR / n_fft
    fps = SR / hop
    nb = flux.shape[1]
    by_time: dict[float, list] = {}
    for p, s, *_ in notes:
        by_time.setdefault(round(s, 4), []).append(p)

    gaps = np.diff(onsets)
    room = np.minimum(np.r_[np.inf, gaps], np.r_[gaps, np.inf])        # distance to the nearest neighbouring onset
    cands = []     # per onset: (times, strengths); the DTW estimate is always one of them
    norm = []      # per onset: (mean, std) of its onset curve in its window, to judge other moments the same way
    own_bins = []  # per onset: the spectrum bins of its notes' pitches (for judging the attack afterwards)
    for (t, e), rm in zip(zip(onsets, est), room):
        win = float(np.clip(SNAP_SPARSE * rm, SNAP_WINDOW, SNAP_MAX))
        bins = []
        for p in by_time.get(round(t, 4), ()):
            f0 = 440.0 * 2 ** ((p - 69) / 12)
            for h in (1, 2, 3):
                b = int(round(f0 * h / binhz))
                if 1 <= b < nb - 1:
                    bins += [b - 1, b, b + 1]
        own_bins.append(bins)
        a, b = max(int((e - win) * fps), 1), min(int((e + win) * fps) + 1, len(flux) - 1)
        if not bins or b - a < 5:
            cands.append((np.array([e]), np.array([0.0])))
            norm.append(None)
            continue
        curve = flux[a:b][:, bins].sum(axis=1)
        norm.append((curve.mean(), curve.std() + 1e-6))
        z = (curve - norm[-1][0]) / norm[-1][1]
        peaks = [i for i in range(1, len(z) - 1) if z[i] >= z[i - 1] and z[i] > z[i + 1] and z[i] > SNAP_PEAK]
        peaks = sorted(peaks, key=lambda i: -z[i])[:SNAP_CANDIDATES]
        times, strength = [], []
        for i in peaks:
            den = z[i - 1] - 2 * z[i] + z[i + 1]
            d = 0.5 * (z[i - 1] - z[i + 1]) / den if den else 0.0
            times.append((a + i + d) / fps - 0.005)
            strength.append(min(z[i], 6.0))
        times.append(e)
        strength.append(0.0)                            # no evidence for "keep the estimate"
        o = np.argsort(times)
        cands.append((np.array(times)[o], np.array(strength)[o]))

    # Viterbi over the candidates
    n = len(onsets)
    score = [cands[0][1].copy()]
    back = []
    for k in range(1, n):
        tk, sk = cands[k]
        tp, _ = cands[k - 1]
        d_est = max(est[k] - est[k - 1], 1e-3)
        dt = tk[:, None] - tp[None, :]                  # (cand k, cand k-1)
        ratio = np.abs(np.log(np.maximum(dt, 1e-3) / d_est))
        pen = SNAP_RATIO_COST * np.maximum(ratio - np.log(SNAP_FREE_RATIO), 0)
        pen = np.where(dt < 0.012, 1e3, pen)            # notes cannot be simultaneous or reversed
        tot = score[-1][None, :] - pen
        j = np.argmax(tot, axis=1)
        score.append(sk + tot[np.arange(len(tk)), j])
        back.append(j)
    idx = [int(np.argmax(score[-1]))]
    for k in range(n - 2, -1, -1):
        idx.append(int(back[k][idx[-1]]))
    idx.reverse()
    # A note written right before the next one (a bass note under the first note of a run) can be played at the
    # very same instant, which the pairwise search above forbids; it then ends up at some earlier, fainter
    # attack of its own bins.  If a note sits far ahead of its neighbour while its own pitches also start at the
    # neighbour's moment, move it there.  That moment may lie outside the window the note's candidates were
    # taken from (the DTW can place such a note in the pause before the run), so it is judged directly.
    tempo = np.ones(n)
    for k in range(n):
        lo, hi = max(k - 4, 0), min(k + 4, n - 1)
        if onsets[hi] > onsets[lo]:
            tempo[k] = (est[hi] - est[lo]) / (onsets[hi] - onsets[lo])
    for k in range(n - 1):
        near = onsets[k + 1] - onsets[k]
        t_k, t_next = cands[k][0][idx[k]], cands[k + 1][0][idx[k + 1]]
        if near < SNAP_NEAR and t_next - t_k > max(SNAP_EARLY, 2.5 * near * tempo[k]):
            tk, sk = cands[k]
            close = [j for j in range(len(tk)) if abs(tk[j] - t_next) <= SNAP_TOGETHER and sk[j] > 0]
            if close:
                idx[k] = max(close, key=lambda j: sk[j])
            elif norm[k] is not None:
                f = int(round((t_next + 0.005) * fps))
                tol = int(round(SNAP_TOGETHER * fps))
                z = (flux[max(f - tol, 0):f + tol + 1][:, own_bins[k]].sum(axis=1) - norm[k][0]) / norm[k][1]
                if len(z) and z.max() > SNAP_PEAK:
                    cands[k] = (np.r_[tk, t_next], np.r_[sk, min(z.max(), 6.0)])
                    idx[k] = len(tk)
    final = np.array([cands[k][0][idx[k]] for k in range(n)])
    # How specific is the attack at the chosen moment?  The onset energy in the note's own bins against the
    # average onset energy of the pitch range: about 1 for an unrelated moment (or noise), well above for a
    # note that really starts there.
    top = min(int(4000.0 / binhz), nb)
    spec = np.zeros(n)
    for k, (t, bins) in enumerate(zip(final, own_bins)):
        f = int(round(t * fps))
        seg = flux[max(f - 2, 0):f + 3, :top]
        own = [b for b in bins if b < top]      # harmonics above the compared range do not count
        if own and len(seg):
            spec[k] = float(seg[:, own].mean() / (seg.mean() + 1e-6))
    # How clear is the attack as an event?  The broadband onset energy there against the typical one of the
    # surrounding seconds.  Pitch specificity says little in sparse, heavy passages (a big chord lights up
    # every bin), but there an attack stands out as an event on its own.
    bf = flux[:, 5:top].sum(axis=1)
    clarity = np.zeros(n)
    w = int(1.5 * fps)
    for k, t in enumerate(final):
        f = int(round(t * fps))
        loc = bf[max(f - w, 0):f + w]
        if len(loc) > 10:
            clarity[k] = (bf[max(f - 2, 0):f + 3].max() - np.median(loc)) / (loc.std() + 1e-9)
    return final, spec, clarity
