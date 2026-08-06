
import json, pickle, warnings
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    roc_auc_score, average_precision_score, precision_recall_curve, confusion_matrix
)
from sklearn.calibration import calibration_curve
import xgboost as xgb

warnings.filterwarnings("ignore")
import train_model_v4 as t4

SCRIPT_DIR = Path(__file__).resolve().parent
OUT_DIR    = SCRIPT_DIR / "eval"
OUT_DIR.mkdir(exist_ok=True)
RS = t4.RANDOM_STATE
CAT_COLS = ["subregion_id", "land_cover_cat"]

def _prep(df, cols):
    X = df[cols].copy()
    for c in CAT_COLS:
        if c in X.columns:
            X[c] = X[c].astype("category")
    return X, df[t4.TARGET].values.astype(np.int32)

def ece(y, p, n_bins=10):
    bins = pd.qcut(p, q=n_bins, duplicates="drop")
    e = 0.0
    for b in bins.categories:
        m = np.asarray(bins == b)
        if m.sum() == 0:
            continue
        e += (m.sum() / len(y)) * abs(y[m].mean() - p[m].mean())
    return float(e)

def main():
    print("=" * 70)
    print("   Model V4 — Probability Recalibration (R1)")
    print("=" * 70)

    df = t4.load_data()
    train_mask = df["fecha_join"] < pd.Timestamp(t4.TEMPORAL_SPLIT_DATE)
    df = t4.add_features(df, train_mask)
    with open(SCRIPT_DIR / "feature_cols_v4.pkl", "rb") as f:
        feature_cols = pickle.load(f)
    _, df_val, df_test = t4.split_sample_and_validate(df)
    X_val,  y_val  = _prep(df_val,  feature_cols)
    X_test, y_test = _prep(df_test, feature_cols)

    model = xgb.XGBClassifier(enable_categorical=True)
    model.load_model(str(SCRIPT_DIR / "xgboost_v4.json"))
    raw_val  = model.predict_proba(X_val)[:, 1]
    raw_test = model.predict_proba(X_test)[:, 1]

    ece_val_raw  = ece(y_val,  raw_val)
    ece_test_raw = ece(y_test, raw_test)
    print(f"\n   Raw ECE   — val {ece_val_raw:.4f}   test {ece_test_raw:.4f}")

    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(raw_val, y_val)
    iso_val  = iso.predict(raw_val)
    iso_test = iso.predict(raw_test)

    eps = 1e-6
    logit = lambda p: np.log(np.clip(p, eps, 1 - eps) / (1 - np.clip(p, eps, 1 - eps)))
    platt = LogisticRegression(C=1e6, solver="lbfgs")
    platt.fit(logit(raw_val).reshape(-1, 1), y_val)
    platt_val  = platt.predict_proba(logit(raw_val).reshape(-1, 1))[:, 1]
    platt_test = platt.predict_proba(logit(raw_test).reshape(-1, 1))[:, 1]

    rows = []
    for name, pv, pt in [("raw", raw_val, raw_test),
                         ("isotonic", iso_val, iso_test),
                         ("platt", platt_val, platt_test)]:
        rows.append({
            "calibrator": name,
            "ece_val":  ece(y_val, pv),
            "ece_test": ece(y_test, pt),
            "auc_test": roc_auc_score(y_test, pt),
            "ap_test":  average_precision_score(y_test, pt),
        })
    comp = pd.DataFrame(rows)
    comp.to_csv(OUT_DIR / "calibration_compare_v4.csv", index=False)
    print("\n   Calibrator comparison:")
    for r in comp.itertuples():
        print(f"     {r.calibrator:<9}  ECE val {r.ece_val:.4f}  test {r.ece_test:.4f}"
              f"   AUC {r.auc_test:.4f}  AP {r.ap_test:.4f}")

    ece_iso_val   = ece(y_val, iso_val)
    ece_platt_val = ece(y_val, platt_val)
    if ece_iso_val < ece_platt_val - 0.005:
        best_name, best_test = "isotonic", iso_test
    else:
        best_name, best_test = "platt", platt_test
    print(f"\n   Selected (lowest val ECE): {best_name}  "
          f"→ test ECE {ece_test_raw:.4f} → {ece(y_test, best_test):.4f}")

    pr, rc, th = precision_recall_curve(y_test, best_test)
    f1 = 2 * pr * rc / (pr + rc + 1e-8)
    f2 = 5 * pr * rc / (4 * pr + rc + 1e-8)
    t_f1 = float(th[min(int(np.argmax(f1)), len(th) - 1)])
    t_f2 = float(th[min(int(np.argmax(f2)), len(th) - 1)])

    def at(t):
        yp = (best_test >= t).astype(int)
        tn, fp, fn, tp = confusion_matrix(y_test, yp).ravel()
        p = tp / (tp + fp) if tp + fp else 0
        r = tp / (tp + fn) if tp + fn else 0
        return {"threshold": t, "precision": p, "recall": r, "tp": int(tp),
                "fp": int(fp), "fn": int(fn), "tn": int(tn)}
    m_f1, m_f2 = at(t_f1), at(t_f2)
    print(f"   Calibrated F2 threshold {t_f2:.3f}  → precision {m_f2['precision']:.3f}"
          f"  recall {m_f2['recall']:.3f}")

    payload = {
        "method": best_name,
        "isotonic": iso if best_name == "isotonic" else None,
        "platt": platt if best_name == "platt" else None,
        "f1_threshold_calibrated": t_f1,
        "f2_threshold_calibrated": t_f2,
        "ece_test_raw": ece_test_raw,
        "ece_test_calibrated": ece(y_test, best_test),
        "note": "Apply to raw model.predict_proba()[:,1]. For platt, transform "
                "via logit first. Maps inflated 1:1-trained scores to real-prevalence "
                "probabilities for the dashboard/API risk_level.",
    }
    with open(SCRIPT_DIR / "calibrator_v4.pkl", "wb") as f:
        pickle.dump(payload, f)

    cal_json = {
        "method": best_name,
        "f1_threshold_calibrated": t_f1,
        "f2_threshold_calibrated": t_f2,
        "ece_test_raw": ece_test_raw,
        "ece_test_calibrated": ece(y_test, best_test),
    }
    if best_name == "platt":
        cal_json["platt"] = {"a": float(platt.coef_[0][0]), "b": float(platt.intercept_[0])}
        cal_json["formula"] = "calibrated = 1/(1+exp(-(a*logit(raw)+b))), logit(p)=log(p/(1-p))"
    else:
        cal_json["isotonic"] = {"x": iso.X_thresholds_.tolist(),
                                "y": iso.y_thresholds_.tolist()}
    json.dump(cal_json, open(SCRIPT_DIR / "calibrator_v4.json", "w"), indent=2)
    print(f"\n   Persisted calibrator_v4.pkl + calibrator_v4.json ({best_name})")

    fig, ax = plt.subplots(figsize=(7, 6))
    ax.plot([0, 1], [0, 1], "k--", alpha=0.4, label="perfect")
    for p, lab, col in [(raw_test, f"raw (ECE {ece_test_raw:.3f})", "#9e9e9e"),
                        (best_test, f"{best_name} (ECE {ece(y_test, best_test):.3f})", "#E8612A")]:
        fp_, mp_ = calibration_curve(y_test, p, n_bins=10, strategy="quantile")
        ax.plot(mp_, fp_, "o-", color=col, label=lab)
    ax.set_title("V4 calibration on held-out test (before vs after)")
    ax.set_xlabel("Mean predicted probability"); ax.set_ylabel("Observed fire fraction")
    ax.legend(); ax.grid(alpha=0.3)
    plt.tight_layout()
    fig.savefig(OUT_DIR / "calibration_v4_after.png", dpi=150)
    plt.close(fig)

    json.dump({"selected": best_name, "ece_test_raw": ece_test_raw,
               "ece_test_calibrated": ece(y_test, best_test),
               "f1_threshold_calibrated": t_f1, "f2_threshold_calibrated": t_f2,
               "metrics_f1": m_f1, "metrics_f2": m_f2},
              open(OUT_DIR / "calibration_summary_v4.json", "w"), indent=2, default=str)

    print("\n" + "=" * 70)
    print(f"   Done. ECE {ece_test_raw:.3f} → {ece(y_test, best_test):.3f}  "
          f"({best_name}). Outputs in {OUT_DIR}")
    print("=" * 70)

if __name__ == "__main__":
    main()
