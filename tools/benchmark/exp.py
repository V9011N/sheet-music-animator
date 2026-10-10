"""Run every training alignment with the current analysis.py (optionally overriding constants) and summarise.

python exp.py TAG [NAME=VALUE ...]
Prints per-recording overall confidence, mean conf of the first/last 5 onsets and Winter Wind ground truth.
Results are cached under TAG; use a new TAG for every variant.
"""
import os
import sys

import json
if __name__ == "__main__":
    os.environ["TAG"] = sys.argv[1]
    os.environ["EXP_OVER"] = json.dumps(dict(a.split("=", 1) for a in sys.argv[2:]))
    os.environ["EXP_SKIP"] = os.environ.get("EXP_SKIP", "")
TAG = os.environ["TAG"]
over = json.loads(os.environ.get("EXP_OVER", "{}"))
sys.argv = sys.argv[:1]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from multiprocessing import Pool  # noqa: E402

import numpy as np  # noqa: E402

PAIRS = [("Alkan", "Alkan"), ("Chaconne", "Kissin"), ("Chaconne", "Hamelin"), ("Ballade 4", "CHOPIN"), ("Ballade 4", "Zimerman"),
         ("Baracarolle", "Zim"), ("Polonaise Fantasy", "Trif"), ("Op 53", "Heroic"), ("Prelude 8", "Kissin"),
         ("Sonata 2", "Pollini"), ("Winter", "Kissin"), ("Dante", "Cho"), ("Mephisto", "Cziffra"), ("Mephisto", "Kissin"),
         ("TE 10", "Lim"), ("TE 10", "Kissin"), ("TE 4", "Lim"), ("TE 4", "S.139"), ("TE 4", "Cziffra"),
         ("Tableux", "Lugansky"), ("Tableux", "Horowitz"),
         ("Moments", "Mom"), ("Op 3 No 2", "Prel"), ("Jeux", "Cho"), ("Scriabin", "Scri"),
         ("Ondine", "M. 55"), ("Ondine", "Carnegie")]


def setup():
    from sheet_music_animator import analysis
    for k, v in over.items():
        setattr(analysis, k, type(getattr(analysis, k))(eval(v)))


def one(pa):
    setup()
    import sync_eval as S
    try:
        xml, aud = S.find(*pa)
        d = S.align(xml, aud, TAG)
    except Exception as e:  # noqa: BLE001
        return pa, None, repr(e)[:120]
    c = np.asarray(d["conf"])
    return pa, (d["overall"], c[:5].mean(), c[-5:].mean(), c.mean()), ""


if __name__ == "__main__":
    skip = [x for x in os.environ.get("EXP_SKIP", "").split(",") if x]
    todo = [pa for pa in PAIRS if not any(k in pa[0] for k in skip)]
    with Pool(6, initializer=setup) as pool:
        res = pool.map(one, todo)
    tot = []
    for (p, a), r, err in res:
        if r is None:
            print(f"{p:18s}{a:10s} ERROR {err}")
            continue
        tot.append(r)
        print(f"{p:18s}{a:10s} overall {r[0]:.3f}  mean {r[3]:.3f}  first5 {r[1]:.2f}  last5 {r[2]:.2f}")
    t = np.array(tot)
    print(f"MEAN overall {t[:, 0].mean():.4f}  mean {t[:, 3].mean():.4f}  first5 {t[:, 1].mean():.3f}  last5 {t[:, 2].mean():.3f}")
    setup()
    import sync_eval as S
    res, _, _ = S.gt_eval(TAG, verbose=False)
    print("GT winter wind:", {k: round(v, 4) for k, v in res.items()})
