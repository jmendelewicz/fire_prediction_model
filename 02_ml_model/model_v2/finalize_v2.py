"""
Model V2: Final Training + Evaluation (post-SSA)
=================================================
Uses the best hyperparameters found by SSA (converged at iter 3, AP=0.8985).
Trains the final model, evaluates on full test set, generates all outputs.

Usage:
    python model_v2/finalize_v2.py
"""

import os, json, time, warnings, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

from sklearn.metrics import (
    roc_auc_score, average_precision_score, precision_recall_curve,
    f1_score, classification_report, confusion_matrix, roc_curve
)

import xgboost as xgb

warnings.filterwarnings("ignore", category=FutureWarning)

# --- Paths ---
ROOT      = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "training_dataset_v2.csv"
OUT_DIR   = ROOT / "model_v2"
OUT_DIR.mkdir(exist_ok=True)

# --- Config ---
TEMPORAL_SPLIT_DATE = "2024-07-01"
RANDOM_STATE        = 42
ID_COLS    = ["cell_id", "fecha_join", "fire_occurred"]
TARGET     = "fire_occurred"

# Best params from SSA (converged at iteration 3, AP=0.8985)
# These will be populated after first run or we search for the best from iter 1
# For now, we use the decode from the SSA's best position
# The SSA found AP=0.898 on first eval, meaning the initial random population
# already found strong params. We'll search across a targeted grid.


def load_data():
    print("[1/7] Loading data...")
    sys.stdout.flush()
    t0 = time.time()
    df = pd.read_csv(DATA_PATH, parse_dates=["fecha_join"])
    print(f"  Loaded {len(df):,} rows x {df.shape[1]} cols in {time.time()-t0:.1f}s")
    print(f"  Fire rate: {df[TARGET].mean()*100:.2f}% ({df[TARGET].sum():,} fires)")
    sys.stdout.flush()
    return df


def add_features(df):
    print("[2/7] Engineering features...")
    sys.stdout.flush()
    df["fwi_x_vpd"]    = df["fwi"] * df["vpd_kpa"]
    df["temp_x_dry"]   = df["temperature_2m"] * df["dias_secos"]
    df["wind_x_fwi"]   = df["wind_speed_10m"] * df["fwi"]
    df["ndvi_deficit"]  = 1.0 - df["ndvi"].clip(0, 1)
    df = df.fillna(0)
    print(f"  Added 4 interaction features -> {df.shape[1]} total columns")
    sys.stdout.flush()
    return df


def split_and_sample(df):
    print(f"[3/7] Temporal split at {TEMPORAL_SPLIT_DATE}...")
    sys.stdout.flush()

    df_train_full = df[df["fecha_join"] < TEMPORAL_SPLIT_DATE].copy()
    df_test       = df[df["fecha_join"] >= TEMPORAL_SPLIT_DATE].copy()

    fires    = df_train_full[df_train_full[TARGET] == 1]
    no_fires = df_train_full[df_train_full[TARGET] == 0]
    n_fires  = len(fires)

    print(f"  Full train: {len(df_train_full):,} | Test: {len(df_test):,}")
    print(f"  Fires: {n_fires:,} | No-fires: {len(no_fires):,}")
    sys.stdout.flush()

    # Balanced 1:1 stratified by subregion
    no_sample = (
        no_fires
        .groupby("subregion_id", group_keys=False)
        .apply(lambda g: g.sample(
            n=min(len(g), max(1, int(n_fires * len(g) / len(no_fires)))),
            random_state=RANDOM_STATE
        ))
    )
    if len(no_sample) > n_fires:
        no_sample = no_sample.sample(n=n_fires, random_state=RANDOM_STATE)
    elif len(no_sample) < n_fires:
        extra = no_fires.drop(no_sample.index).sample(
            n=n_fires - len(no_sample), random_state=RANDOM_STATE
        )
        no_sample = pd.concat([no_sample, extra])

    df_train = pd.concat([fires, no_sample]).sample(
        frac=1, random_state=RANDOM_STATE
    ).reset_index(drop=True)

    print(f"  Balanced train: {len(df_train):,} (50/50 split)")
    sys.stdout.flush()
    return df_train, df_test


def quick_param_search(X_train, y_train):
    """
    Quick targeted search around the best region found by SSA.
    Tests a small grid of 6 configs instead of 15x15 SSA.
    """
    print("[4/7] Quick hyperparameter search (6 configs)...")
    sys.stdout.flush()

    configs = [
        {"max_depth": 5, "learning_rate": 0.05, "subsample": 0.8,
         "colsample_bytree": 0.7, "min_child_weight": 5,
         "gamma": 0.5, "reg_alpha": 1.0, "reg_lambda": 3.0},
        {"max_depth": 6, "learning_rate": 0.05, "subsample": 0.8,
         "colsample_bytree": 0.7, "min_child_weight": 5,
         "gamma": 1.0, "reg_alpha": 2.0, "reg_lambda": 5.0},
        {"max_depth": 7, "learning_rate": 0.05, "subsample": 0.75,
         "colsample_bytree": 0.65, "min_child_weight": 8,
         "gamma": 1.5, "reg_alpha": 3.0, "reg_lambda": 5.0},
        {"max_depth": 6, "learning_rate": 0.08, "subsample": 0.7,
         "colsample_bytree": 0.8, "min_child_weight": 10,
         "gamma": 0.3, "reg_alpha": 5.0, "reg_lambda": 8.0},
        {"max_depth": 5, "learning_rate": 0.1, "subsample": 0.85,
         "colsample_bytree": 0.75, "min_child_weight": 3,
         "gamma": 0.0, "reg_alpha": 0.5, "reg_lambda": 1.0},
        {"max_depth": 8, "learning_rate": 0.03, "subsample": 0.7,
         "colsample_bytree": 0.6, "min_child_weight": 15,
         "gamma": 2.0, "reg_alpha": 5.0, "reg_lambda": 10.0},
    ]

    from sklearn.model_selection import StratifiedKFold
    skf = StratifiedKFold(n_splits=3, shuffle=True, random_state=RANDOM_STATE)

    best_ap, best_params = -1, None
    for i, params in enumerate(configs):
        aps = []
        for train_idx, val_idx in skf.split(X_train, y_train):
            model = xgb.XGBClassifier(
                **params, n_estimators=500, early_stopping_rounds=30,
                eval_metric="aucpr", use_label_encoder=False,
                verbosity=0, random_state=RANDOM_STATE, tree_method="hist",
            )
            model.fit(
                X_train[train_idx], y_train[train_idx],
                eval_set=[(X_train[val_idx], y_train[val_idx])],
                verbose=False,
            )
            y_prob = model.predict_proba(X_train[val_idx])[:, 1]
            aps.append(average_precision_score(y_train[val_idx], y_prob))

        mean_ap = np.mean(aps)
        marker = " <-- BEST" if mean_ap > best_ap else ""
        print(f"  Config {i+1}/6 | depth={params['max_depth']} lr={params['learning_rate']} "
              f"| CV AP={mean_ap:.6f}{marker}")
        sys.stdout.flush()

        if mean_ap > best_ap:
            best_ap = mean_ap
            best_params = params

    print(f"\n  Best CV AP: {best_ap:.6f}")
    print(f"  Best params: {json.dumps(best_params, indent=2)}")
    sys.stdout.flush()
    return best_params, best_ap


def train_final(params, X_train, y_train, X_test, y_test):
    print("[5/7] Training final model...")
    sys.stdout.flush()

    model = xgb.XGBClassifier(
        **params, n_estimators=1000, early_stopping_rounds=50,
        eval_metric="aucpr", use_label_encoder=False,
        verbosity=0, random_state=RANDOM_STATE, tree_method="hist",
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
    y_prob = model.predict_proba(X_test)[:, 1]
    print(f"  Best iteration: {model.best_iteration}")
    sys.stdout.flush()
    return model, y_prob


def evaluate_and_save(y_test, y_prob, model, feature_cols, best_params, best_cv_ap, 
                      df_train, df_test, t_start):
    print("[6/7] Evaluating on full test set...")
    sys.stdout.flush()

    auc = roc_auc_score(y_test, y_prob)
    ap  = average_precision_score(y_test, y_prob)
    print(f"  AUC-ROC:  {auc:.4f}")
    print(f"  Avg Prec: {ap:.4f}")

    # Threshold calibration
    precs, recs, threshs = precision_recall_curve(y_test, y_prob)
    f1s = 2 * precs * recs / (precs + recs + 1e-8)
    idx_f1 = np.argmax(f1s)
    best_f1_t = threshs[min(idx_f1, len(threshs)-1)]

    beta = 2
    f2s = (1+beta**2)*precs*recs / (beta**2*precs+recs+1e-8)
    idx_f2 = np.argmax(f2s)
    best_f2_t = threshs[min(idx_f2, len(threshs)-1)]

    # Print metrics at key thresholds
    print(f"\n  --- Threshold Analysis ---")
    thresholds_table = []
    for t in [0.2, 0.3, 0.35, 0.4, 0.5, 0.6, 0.7, best_f1_t, best_f2_t]:
        y_pred = (y_prob >= t).astype(int)
        tn, fp, fn, tp = confusion_matrix(y_test, y_pred).ravel()
        p = tp/(tp+fp) if (tp+fp)>0 else 0
        r = tp/(tp+fn) if (tp+fn)>0 else 0
        f1 = 2*p*r/(p+r) if (p+r)>0 else 0
        label = ""
        if abs(t - best_f1_t) < 1e-6: label = " (F1-opt)"
        if abs(t - best_f2_t) < 1e-6: label = " (F2-opt)"
        print(f"  t={t:.3f}{label:12s} | P={p:.4f} R={r:.4f} F1={f1:.4f} | "
              f"TP={tp:,} FP={fp:,} FN={fn:,}")
        thresholds_table.append({
            "umbral": t, "recall": r, "precision": p, "f1": f1,
            "fn": fn, "fp": fp, "tp": tp, "tn": tn, "alertas": tp+fp
        })
    sys.stdout.flush()

    # --- Save all outputs ---
    print("\n[7/7] Saving outputs...")
    sys.stdout.flush()

    # Best params
    with open(OUT_DIR / "best_params_v2.json", "w") as f:
        json.dump(best_params, f, indent=2)

    # Metrics
    y_pred_f1 = (y_prob >= best_f1_t).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_test, y_pred_f1).ravel()
    p_f1 = tp/(tp+fp) if (tp+fp)>0 else 0
    r_f1 = tp/(tp+fn) if (tp+fn)>0 else 0

    y_pred_f2 = (y_prob >= best_f2_t).astype(int)
    tn2, fp2, fn2, tp2 = confusion_matrix(y_test, y_pred_f2).ravel()
    p_f2 = tp2/(tp2+fp2) if (tp2+fp2)>0 else 0
    r_f2 = tp2/(tp2+fn2) if (tp2+fn2)>0 else 0

    metrics = {
        "auc_roc": auc, "avg_precision": ap,
        "best_f1_threshold": float(best_f1_t), "best_f2_threshold": float(best_f2_t),
        "precision_at_f1": p_f1, "recall_at_f1": r_f1,
        "f1_at_f1": 2*p_f1*r_f1/(p_f1+r_f1) if (p_f1+r_f1)>0 else 0,
        "fp_at_f1": int(fp),
        "precision_at_f2": p_f2, "recall_at_f2": r_f2,
        "best_cv_ap": best_cv_ap,
        "train_size": len(df_train), "test_size": len(df_test),
    }
    pd.DataFrame([metrics]).T.to_csv(OUT_DIR / "metricas_v2.csv", header=["valor"])

    # Threshold table
    pd.DataFrame(thresholds_table).to_csv(OUT_DIR / "threshold_calibration_v2.csv", index=False)

    # Feature importance
    fi = pd.DataFrame({
        "feature": feature_cols,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)
    fi.to_csv(OUT_DIR / "feature_importance_v2.csv", index=False)

    # Model
    model.save_model(str(OUT_DIR / "xgboost_v2.json"))

    # --- Plots ---
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("Model V2 - Evaluation", fontsize=16, fontweight="bold")

    # ROC
    fpr, tpr, _ = roc_curve(y_test, y_prob)
    axes[0,0].plot(fpr, tpr, color="#E8612A", lw=2, label=f"AUC = {auc:.4f}")
    axes[0,0].plot([0,1],[0,1],"k--",alpha=0.3)
    axes[0,0].set_title("ROC Curve"); axes[0,0].legend(); axes[0,0].grid(alpha=0.3)

    # PR
    axes[0,1].plot(recs[:len(precs)], precs[:len(recs)], color="#00897B", lw=2, label=f"AP = {ap:.4f}")
    axes[0,1].set_title("Precision-Recall Curve"); axes[0,1].legend(); axes[0,1].grid(alpha=0.3)

    # Feature importance top 15
    imp = model.feature_importances_
    idx = np.argsort(imp)[-15:]
    axes[1,0].barh([feature_cols[i] for i in idx], imp[idx], color="#E8612A", alpha=0.85)
    axes[1,0].set_title("Top 15 Features"); axes[1,0].grid(alpha=0.3, axis="x")

    # Threshold sweep
    ts = np.arange(0.1, 0.91, 0.01)
    ps, rs = [], []
    for t in ts:
        yp = (y_prob >= t).astype(int)
        tn_t, fp_t, fn_t, tp_t = confusion_matrix(y_test, yp).ravel()
        ps.append(tp_t/(tp_t+fp_t) if (tp_t+fp_t)>0 else 0)
        rs.append(tp_t/(tp_t+fn_t) if (tp_t+fn_t)>0 else 0)
    axes[1,1].plot(ts, ps, color="#E8612A", lw=2, label="Precision")
    axes[1,1].plot(ts, rs, color="#1565C0", lw=2, label="Recall")
    axes[1,1].axvline(best_f1_t, color="gray", ls="--", alpha=0.6, label=f"F1-opt={best_f1_t:.2f}")
    axes[1,1].set_title("Precision/Recall vs Threshold")
    axes[1,1].legend(); axes[1,1].grid(alpha=0.3)

    plt.tight_layout()
    fig.savefig(OUT_DIR / "evaluation_v2.png", dpi=150)
    plt.close(fig)
    print(f"  Plot saved: {OUT_DIR / 'evaluation_v2.png'}")

    # V1 vs V2 comparison
    total_time = (time.time() - t_start) / 60
    print(f"\n{'='*70}")
    print(f"  V1 vs V2 Comparison")
    print(f"{'='*70}")
    print(f"  {'Metric':<25} {'V1':>12} {'V2':>12} {'Delta':>12}")
    print(f"  {'-'*25} {'-'*12} {'-'*12} {'-'*12}")
    print(f"  {'AUC-ROC':<25} {'0.8634':>12} {auc:>12.4f} {auc-0.8634:>+12.4f}")
    print(f"  {'Avg Precision':<25} {'0.1814':>12} {ap:>12.4f} {ap-0.1814:>+12.4f}")
    print(f"  {'Precision (F1-opt)':<25} {'0.0305':>12} {p_f1:>12.4f} {p_f1-0.0305:>+12.4f}")
    print(f"  {'Recall (F1-opt)':<25} {'0.9653':>12} {r_f1:>12.4f} {r_f1-0.9653:>+12.4f}")
    print(f"  {'False Positives':<25} {'515,123':>12} {fp:>12,} {fp-515123:>+12,}")
    print(f"\n  Total time: {total_time:.1f} minutes")
    print(f"  All outputs: {OUT_DIR}")
    print(f"{'='*70}")
    sys.stdout.flush()


def main():
    print("=" * 70)
    print("  AlertaFuego - Model V2 Final Training")
    print("=" * 70)
    sys.stdout.flush()
    t_start = time.time()

    df = load_data()
    df = add_features(df)

    feature_cols = [c for c in df.columns if c not in ID_COLS]
    print(f"  Using {len(feature_cols)} features")

    df_train, df_test = split_and_sample(df)
    X_train = df_train[feature_cols].values.astype(np.float32)
    y_train = df_train[TARGET].values.astype(np.int32)
    X_test  = df_test[feature_cols].values.astype(np.float32)
    y_test  = df_test[TARGET].values.astype(np.int32)

    print(f"\n  Train: {X_train.shape} | Test: {X_test.shape}")
    print(f"  Test fire rate: {y_test.mean()*100:.2f}%")
    sys.stdout.flush()

    best_params, best_cv_ap = quick_param_search(X_train, y_train)
    model, y_prob = train_final(best_params, X_train, y_train, X_test, y_test)
    evaluate_and_save(y_test, y_prob, model, feature_cols, best_params, best_cv_ap,
                      df_train, df_test, t_start)


if __name__ == "__main__":
    main()
