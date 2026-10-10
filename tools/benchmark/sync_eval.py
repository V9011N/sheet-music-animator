"""Align every training score to its recording(s) with the app's align_score and judge the result.

python sync_eval.py align <piece substr> [audio substr]     -> cache/<key>.pkl  (alignment + notes)
python sync_eval.py gt                                      -> Winter Wind vs. ground truth
python sync_eval.py viz <piece substr> [audio substr] [t0 t1 ...]  -> spectrogram overlays
"""
import hashlib
import json
import os
import pickle
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TRAIN = ROOT / "01_XMLs_PDFs_MP3s_for_training"
HERE = Path(__file__).parent / "out"
CACHE = HERE / "cache"
CACHE.mkdir(parents=True, exist_ok=True)
# note times worked out by hand for one recording (cols_tau.json, t2.npy, meta.json of the first renderer)
WW = Path(os.environ.get("SMA_GROUND_TRUTH", ROOT.parent / "Sheet Music Animator" / "debug files" / "Winter Wind renderer" / "data"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from sheet_music_animator import analysis  # noqa: E402
from sheet_music_animator.engraver import engrave  # noqa: E402

TAG = os.environ.get("TAG", "base")


def find(piece, audio_sub=None, xml_sub=None):
    d = next(p for p in sorted(TRAIN.iterdir()) if piece.lower() in p.name.lower())
    xmls = sorted(d.glob("*.mscz")) + sorted(d.glob("*.mxl")) if ENGINE == "musescore" else sorted(d.glob("*.mxl"))
    xml = next((x for x in xmls if xml_sub and xml_sub.lower() in x.name.lower()), xmls[0])
    auds = sorted(d.glob("*.mp3"))
    aud = next((a for a in auds if audio_sub and audio_sub.lower() in a.name.lower()), auds[0])
    return xml, aud


def key(xml, aud):
    return hashlib.sha1(f"{xml}|{aud}".encode()).hexdigest()[:10]


ENGINE = os.environ.get("ENGINE", "verovio")     # "musescore": MuseScore lays the score out and plays it


def score_of(xml):
    p = CACHE / f"score_{ENGINE}_{key(xml, '')}.pkl"
    if p.exists():
        return pickle.load(open(p, "rb"))
    if ENGINE == "musescore":
        from sheet_music_animator.msengraver import engrave_musescore
        s = engrave_musescore(str(xml))
    else:
        s = engrave(str(xml), measures_per_line=4)
    d = {"notes": s.notes, "rolls": s.rolls, "measures": s.measures, "duration": s.duration}
    pickle.dump(d, open(p, "wb"))
    return d


def align(xml, aud, tag=TAG):
    p = CACHE / f"al_{tag}_{key(xml, aud)}.pkl"
    if p.exists():
        return pickle.load(open(p, "rb"))
    sc = score_of(xml)
    t0 = time.time()
    al = analysis.align_score(sc["notes"], str(aud), rolls=sc["rolls"])
    d = {"nominal": al.nominal, "actual": al.actual, "shifts": al.shifts, "conf": al.confidence,
         "ev": al.evidence, "overall": al.overall, "secs": time.time() - t0, "xml": str(xml), "aud": str(aud)}
    pickle.dump(d, open(p, "wb"))
    return d


# ------------------------------------------------------------------ ground truth (Winter Wind / Kissin)
def gt_eval(tag=TAG, verbose=True):
    xml, aud = find("Winter")
    al = align(xml, aud, tag)
    sc = score_of(xml)
    cols = json.load(open(WW / "cols_tau.json"))
    meta = json.load(open(WW / "meta.json"))
    gtt = np.load(WW / "t2.npy") / meta["fps"] + meta["T0"]
    # the GT audio file is the same recording?  (same size as the training mp3)
    # score onsets per measure with pitch sets
    mt = [t for t, _ in sc["measures"]]
    by_on = {}
    for p, s, e, *_ in sc["notes"]:
        by_on.setdefault(round(s, 4), set()).add(p)
    ons = sorted(by_on)
    f = lambda t: float(np.interp(t, al["nominal"], al["actual"]))
    meas_of = lambda t: int(np.searchsorted(mt, t + 1e-6, side="right"))   # 1-based
    S = {}
    for t in ons:
        S.setdefault(meas_of(t), []).append((t, by_on[t]))
    G = {}
    for c, t in zip(cols, gtt):
        if c["grace"]:
            continue
        ps = {s["midi"] for s in c["sound"]}
        G.setdefault(c["m"], []).append((t, ps))
    errs, pairs = [], []
    for m in sorted(G):
        a, b = S.get(m, []), G[m]
        if not a:
            continue
        # DP alignment by pitch overlap
        n, k = len(a), len(b)
        D = np.zeros((n + 1, k + 1))
        D[:, 0] = np.arange(n + 1) * 0.6
        D[0, :] = np.arange(k + 1) * 0.6
        for i in range(1, n + 1):
            for j in range(1, k + 1):
                sa, sb = a[i - 1][1], b[j - 1][1]
                c = 1 - len(sa & sb) / max(len(sa | sb), 1)
                D[i, j] = min(D[i - 1, j - 1] + c, D[i - 1, j] + 0.6, D[i, j - 1] + 0.6)
        i, j = n, k
        while i and j:
            sa, sb = a[i - 1][1], b[j - 1][1]
            c = 1 - len(sa & sb) / max(len(sa | sb), 1)
            if abs(D[i, j] - (D[i - 1, j - 1] + c)) < 1e-9:
                if c < 0.5:
                    pairs.append((a[i - 1][0], b[j - 1][0], m))
                i, j = i - 1, j - 1
            elif abs(D[i, j] - (D[i - 1, j] + 0.6)) < 1e-9:
                i -= 1
            else:
                j -= 1
    pairs.sort()
    est = np.array([f(a) for a, _, _ in pairs])
    gt = np.array([b for _, b, _ in pairs])
    # constant offset between the two decodes of the audio (GT may be relative to a trimmed file)
    off = np.median(est - gt)
    err = np.abs(est - gt - off)
    res = {"pairs": len(pairs), "offset": off, "w30": float(np.mean(err < 0.03)), "w50": float(np.mean(err < 0.05)),
           "w120": float(np.mean(err < 0.12)), "w300": float(np.mean(err < 0.3)), "median_ms": float(np.median(err) * 1000),
           "overall": al["overall"]}
    if verbose:
        print(json.dumps(res, indent=1))
        bad = [(m, a, e) for (a, b, m), e in zip(pairs, err) if e > 0.12]
        from collections import Counter
        print("measures with >120ms errors:", Counter(m for m, _, _ in bad).most_common(25))
    return res, pairs, err


# ------------------------------------------------------------------ spectrogram overlay
def spectro(aud, fps=100):
    """(level, attack) per semitone, 21..108, `fps` frames/s: a long window below MIDI 55, a short one above."""
    p = CACHE / f"spec2_{key(aud, fps)}.npz"
    if p.exists():
        d = np.load(p)
        return d["L"], d["A"]
    y = analysis.decode_audio(str(aud))
    hop = analysis.SR // fps
    out = np.zeros((len(y) // hop + 1, 88), np.float32)
    for n_fft, lo_m, hi_m in ((8192, 21, 54), (2048, 55, 108)):
        mag = analysis.stft_mag(y, n_fft, hop)[:len(out)]
        f = np.fft.rfftfreq(n_fft, 1 / analysis.SR)
        for midi in range(lo_m, hi_m + 1):
            lo, hi = 440 * 2 ** ((midi - 69.5) / 12), 440 * 2 ** ((midi - 68.5) / 12)
            sel = (f >= lo) & (f < hi)
            idx = np.nonzero(sel)[0] if sel.any() else [int(round((lo + hi) / 2 / f[1]))]
            out[:len(mag), midi - 21] = mag[:, idx].max(axis=1)
    L = np.log1p(40 * out / (np.percentile(out, 99.5) + 1e-9))
    A = np.zeros_like(L)
    A[2:] = np.maximum(L[2:] - np.maximum(L[1:-1], L[:-2]), 0)
    np.savez(p, L=L, A=A)
    return L, A


def viz(xml, aud, windows, tag=TAG, out=None, pxs=150, tile=12.0):
    al = align(xml, aud, tag)
    sc = score_of(xml)
    L, A = spectro(aud)
    fps = analysis.SR / (analysis.SR // 100)     # the exact frame rate of the hop (not 100: it drifts)
    Ln = np.clip(L / np.percentile(L, 99.7), 0, 1)
    An = np.clip(A / np.percentile(A[A > 0], 99.0), 0, 1)
    f = lambda t: np.interp(t, al["nominal"], al["actual"])
    out = Path(out or HERE / "viz")
    out.mkdir(exist_ok=True)
    PH = 7
    files = []
    tiles = []
    for t0, t1 in windows:
        s = t0
        while s < t1 - 1e-6:
            tiles.append((s, min(s + tile, t1)))
            s += tile
    for t0, t1 in tiles:
        W = int((t1 - t0) * pxs)
        img = np.zeros((88 * PH + 30, W, 3), np.uint8)
        fi = np.clip(((np.arange(W) / pxs + t0) * fps).astype(int), 0, len(L) - 1)
        g = np.repeat((Ln[fi] * 170).T[::-1], PH, axis=0)
        r = np.repeat((An[fi] * 255).T[::-1], PH, axis=0)
        img[:88 * PH, :, 0] = g
        img[:88 * PH, :, 1] = g
        img[:88 * PH, :, 2] = np.maximum(g, r)
        for p, s, e, *_ in sc["notes"]:
            ts, te = f(s), f(e)
            if te < t0 or ts > t1 or not 21 <= p <= 108:
                continue
            x0, x1 = int((ts - t0) * pxs), int((te - t0) * pxs)
            y = (108 - p) * PH
            cv2.rectangle(img, (x0, y), (max(x1, x0 + 2), y + PH - 1), (255, 200, 0), 1)
            cv2.line(img, (x0, y - 1), (x0, y + PH), (0, 255, 0), 2)
        for t, c in zip(al["actual"], al["conf"]):
            if t0 <= t <= t1:
                x = int((t - t0) * pxs)
                cv2.line(img, (x, 88 * PH + 2), (x, 88 * PH + 28), (0, int(255 * c), int(255 * (1 - c))), 2)
        for t, n in sc["measures"]:
            x = int((f(t) - t0) * pxs)
            if 0 <= x < W:
                cv2.line(img, (x, 0), (x, 12), (255, 255, 0), 1)
                cv2.putText(img, str(n), (x + 2, 11), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 0), 1)
        for s in np.arange(np.ceil(t0), t1, 1.0):
            x = int((s - t0) * pxs)
            cv2.line(img, (x, 88 * PH), (x, 88 * PH + 6), (255, 255, 255), 1)
            cv2.putText(img, f"{s:.0f}", (x + 2, 88 * PH - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (200, 200, 200), 1)
        for m in range(24, 109, 12):     # C lines
            y = (108 - m) * PH + PH
            img[y, ::6] = (90, 60, 60)
        name = f"{xml.parent.name[:14]}_{aud.stem[:8]}_{tag}_{t0:06.1f}.png".replace(" ", "_")
        cv2.imwrite(str(out / name), img)
        files.append(out / name)
        print(out / name)
    return files


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "align":
        xml, aud = find(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        d = align(xml, aud)
        print(f"{xml.parent.name} | {aud.name[:50]} | overall {d['overall']:.3f} | {d['secs']:.0f}s")
    elif cmd == "gt":
        gt_eval()
    elif cmd == "viz":
        xml, aud = find(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3][0].isdigit() else None)
        nums = [float(x) for x in sys.argv[3:] if x.replace(".", "").isdigit()]
        viz(xml, aud, list(zip(nums[0::2], nums[1::2])))


def worst(xml, aud, tag=TAG, span=10.0, k=3):
    """The k windows of `span` seconds (recording time) with the lowest mean confidence."""
    al = align(xml, aud, tag)
    t, c = np.asarray(al["actual"]), np.asarray(al["conf"])
    out = []
    for s in np.arange(t[0], t[-1] - span / 2, span / 2):
        m = (t >= s) & (t < s + span)
        if m.sum() >= 5:
            out.append((float(c[m].mean()), float(s), int(m.sum())))
    out.sort()
    picked = []
    for v, s, n in out:
        if all(abs(s - p[1]) >= span for p in picked):
            picked.append((v, s, n))
        if len(picked) == k:
            break
    return picked


if __name__ == "__main__" and sys.argv[1] == "worst":
    pairs = [l.split("|") for l in sys.argv[2].split(";")]
    for p, a in pairs:
        xml, aud = find(p, a)
        try:
            print(f"{p:18s} {a:10s}", " ".join(f"[{s:6.1f}s c={v:.2f} n={n}]" for v, s, n in worst(xml, aud)))
        except FileNotFoundError:
            print(p, a, "no alignment")


def conf_quality(tag=TAG):
    """How well the per-onset confidence tells right from wrong onsets on the Winter Wind ground truth: AUC of
    low confidence predicting an error over 50 / 120 ms, and the mean confidence of each group."""
    res, pairs, err = gt_eval(tag, verbose=False)
    xml, aud = find("Winter")
    al = align(xml, aud, tag)
    nom = np.asarray(al["nominal"])
    conf = np.array([al["conf"][int(np.argmin(np.abs(nom - a)))] for a, _, _ in pairs])

    def auc(bad):
        pos, neg = conf[bad], conf[~bad]
        if not len(pos) or not len(neg):
            return float("nan")
        return float(np.mean([(neg > p).mean() + 0.5 * (neg == p).mean() for p in pos]))
    out = {}
    for thr in (0.05, 0.12):
        bad = err > thr
        out[f"auc{int(thr * 1000)}"] = round(auc(bad), 3)
        out[f"conf_ok{int(thr * 1000)}"] = round(float(conf[~bad].mean()), 3)
        out[f"conf_bad{int(thr * 1000)}"] = round(float(conf[bad].mean()), 3) if bad.any() else None
    return out
