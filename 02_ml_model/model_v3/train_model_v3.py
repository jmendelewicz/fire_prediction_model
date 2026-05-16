"""
Model V2: Improved Fire Prediction  XGBoost + SSA
===================================================
Balanced 1:1 sampling, enhanced features, deeper trees with regularization,
and SSA hyperparameter tuning. Follows the methodology of Wang et al. (2026).

Usage:
    python model_v2/train_model_v2.py

Expected time: ~10-15 minutes on a modern CPU.
"""

import os, json, time, warnings
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import (
    roc_auc_score, average_precision_score, precision_recall_curve,
    f1_score, classification_report, confusion_matrix, roc_curve
)

import xgboost as xgb

warnings.filterwarnings("ignore", category=FutureWarning)

#  Paths 
ROOT      = Path(__file__).resolve().parent.parent
DATA_PATH = Path(r"D:\Prueba técnica\fire_prediction_model\save_gold.csv")
OUT_DIR   = Path(r"D:\Prueba técnica\fire_prediction_model\02_ml_model\model_v3")
OUT_DIR.mkdir(exist_ok=True)

#  Configuration 
TEMPORAL_SPLIT_DATE = "2024-07-01"
RANDOM_STATE        = 42
N_SPARROWS          = 15
N_ITER_SSA          = 15

# Feature columns (original 35 - identifiers + 4 new interaction features)
ID_COLS    = ["cell_id", "fecha_join", "fire_occurred"]
TARGET     = "fire_occurred"

#  SSA Search Space 
PARAM_BOUNDS = {
    "max_depth":        (4,  8),
    "learning_rate":    (0.02, 0.15),
    "subsample":        (0.6, 0.9),
    "colsample_bytree": (0.5, 0.9),
    "min_child_weight": (3,  20),
    "gamma":            (0.0, 5.0),
    "reg_alpha":        (0.1, 10.0),
    "reg_lambda":       (0.5, 10.0),
}


# 
# 1. LOAD DATA
# 
def load_data():
    print(" Loading data...")
    t0 = time.time()
    df = pd.read_csv(DATA_PATH, parse_dates=["fecha_join"])
    n_before = len(df)
    df = df.drop_duplicates(subset=["cell_id", "fecha_join"])
    n_dupes = n_before - len(df)
    if n_dupes > 0:
        print(f"   WARNING: Dropped {n_dupes:,} duplicate rows")
    print(f"   Loaded {len(df):,} rows  {df.shape[1]} cols in {time.time()-t0:.1f}s")
    print(f"   Fire rate: {df[TARGET].mean()*100:.2f}% ({df[TARGET].sum():,} fires)")
    return df


# 
# 2. FEATURE ENGINEERING
# 
def add_features(df):
    """Add lightweight interaction features inspired by the reference paper."""
    print(" Engineering features...")

    # Interaction: FWI  VPD (compound fire weather + atmospheric dryness)
    df["fwi_x_vpd"] = df["fwi"] * df["vpd_kpa"]

    # Interaction: temperature  consecutive dry days
    df["temp_x_dry"] = df["temperature_2m"] * df["dias_secos"]

    # Interaction: wind  FWI (fire spread potential)
    df["wind_x_fwi"] = df["wind_speed_10m"] * df["fwi"]

    # Vegetation dryness proxy: Drop from historical mean (addressing feedback)
    df["ndvi_anomaly"] = df["ndvi"] - df.groupby("cell_id")["ndvi"].transform("mean")

    # Fill any NaN from interactions
    df = df.fillna(0)

    print(f"   Added 4 interaction features  {df.shape[1]} total columns")
    return df


# 
# 3. TEMPORAL SPLIT + BALANCED SAMPLING
# 
def split_and_sample(df):
    """
    Temporal train/test split + balanced 1:1 sampling on train set only.
    Test set is kept in full for realistic evaluation.
    """
    print(f"  Temporal split at {TEMPORAL_SPLIT_DATE}...")

    train_mask = df["fecha_join"] < TEMPORAL_SPLIT_DATE
    test_mask  = df["fecha_join"] >= TEMPORAL_SPLIT_DATE

    df_train_full = df[train_mask].copy()
    df_test       = df[test_mask].copy()

    print(f"   Full train: {len(df_train_full):,} rows  |  Test: {len(df_test):,} rows")

    # Balanced 1:1 sampling on train
    fires     = df_train_full[df_train_full[TARGET] == 1]
    no_fires  = df_train_full[df_train_full[TARGET] == 0]
    n_fires   = len(fires)

    print(f"   Train fires: {n_fires:,}  |  Train no-fires: {len(no_fires):,}")

    # Stratified sample of negatives by subregion_id
    no_fires_sample = (
        no_fires
        .groupby("subregion_id", group_keys=False)
        .apply(lambda g: g.sample(
            n=min(len(g), max(1, int(n_fires * len(g) / len(no_fires)))),
            random_state=RANDOM_STATE
        ))
    )
    # Adjust to exact 1:1 if needed
    if len(no_fires_sample) > n_fires:
        no_fires_sample = no_fires_sample.sample(n=n_fires, random_state=RANDOM_STATE)
    elif len(no_fires_sample) < n_fires:
        extra = no_fires.drop(no_fires_sample.index).sample(
            n=n_fires - len(no_fires_sample), random_state=RANDOM_STATE
        )
        no_fires_sample = pd.concat([no_fires_sample, extra])

    df_train_balanced = pd.concat([fires, no_fires_sample]).sample(
        frac=1, random_state=RANDOM_STATE
    ).reset_index(drop=True)

    print(f"   Balanced train: {len(df_train_balanced):,} rows "
          f"(fire={df_train_balanced[TARGET].sum():,} / "
          f"no_fire={len(df_train_balanced) - df_train_balanced[TARGET].sum():,})")

    return df_train_balanced, df_test


def prepare_xy(df, feature_cols):
    X = df[feature_cols].copy()
    if "subregion_id" in X.columns:
        X["subregion_id"] = X["subregion_id"].astype("category")
    if "land_cover_cat" in X.columns:
        X["land_cover_cat"] = X["land_cover_cat"].astype("category")
    y = df[TARGET].values.astype(np.int32)
    return X, y


# 
# 4. SPARROW SEARCH ALGORITHM (SSA)
# 
def decode_sparrow(position):
    """Map continuous [0,1] vector to XGBoost hyperparameters."""
    params = {}
    keys = list(PARAM_BOUNDS.keys())
    for i, k in enumerate(keys):
        lo, hi = PARAM_BOUNDS[k]
        val = lo + position[i] * (hi - lo)
        if k in ("max_depth", "min_child_weight"):
            val = int(round(val))
        params[k] = val
    return params


def evaluate_params(params, X_train, y_train):
    """3-fold CV average precision score."""
    skf = StratifiedKFold(n_splits=3, shuffle=True, random_state=RANDOM_STATE)
    aps = []
    for train_idx, val_idx in skf.split(X_train, y_train):
        model = xgb.XGBClassifier(
            **params,
            n_estimators=500,
            early_stopping_rounds=30,
            eval_metric="aucpr",
            use_label_encoder=False,
            enable_categorical=True,
            verbosity=0,
            random_state=RANDOM_STATE,
            tree_method="hist",
        )
        model.fit(
            X_train.iloc[train_idx], y_train[train_idx],
            eval_set=[(X_train.iloc[val_idx], y_train[val_idx])],
            verbose=False,
        )
        y_prob = model.predict_proba(X_train.iloc[val_idx])[:, 1]
        aps.append(average_precision_score(y_train[val_idx], y_prob))
    return np.mean(aps)


def ssa_optimize(X_train, y_train):
    """
    Sparrow Search Algorithm for XGBoost hyperparameter optimization.
    """
    print(f"\n SSA Optimization: {N_SPARROWS} sparrows  {N_ITER_SSA} iterations")
    print(f"   Training on {X_train.shape[0]:,} rows  {X_train.shape[1]} features\n")

    n_dim = len(PARAM_BOUNDS)
    rng   = np.random.RandomState(RANDOM_STATE)

    # Initialize population
    positions = rng.rand(N_SPARROWS, n_dim)
    fitness   = np.full(N_SPARROWS, -np.inf)

    best_pos     = None
    best_fitness = -np.inf
    convergence  = []

    t0 = time.time()

    SSA_PATIENCE = 3  # Stop if no improvement in N consecutive iterations
    stale_count  = 0

    for it in range(N_ITER_SSA):
        prev_best = best_fitness

        # Evaluate all sparrows
        for i in range(N_SPARROWS):
            params = decode_sparrow(positions[i])
            try:
                score = evaluate_params(params, X_train, y_train)
            except Exception:
                score = 0.0
            fitness[i] = score

            if score > best_fitness:
                best_fitness = score
                best_pos = positions[i].copy()

        convergence.append(best_fitness)

        # Early convergence check
        if best_fitness <= prev_best + 1e-5:
            stale_count += 1
        else:
            stale_count = 0

        # Sort by fitness (best first)
        order     = np.argsort(-fitness)
        positions = positions[order]
        fitness   = fitness[order]

        n_producers = max(1, N_SPARROWS // 5)  # top 20% are producers
        n_scroungers = N_SPARROWS - n_producers

        # Update producers (exploration)
        for i in range(n_producers):
            if rng.rand() < 0.8:
                positions[i] += rng.randn(n_dim) * 0.1 * (1 - it / N_ITER_SSA)
            else:
                positions[i] = rng.rand(n_dim)

        # Update scroungers (follow best producers)
        for i in range(n_producers, N_SPARROWS):
            if rng.rand() < 0.5:
                # Follow the best producer
                j = rng.randint(0, n_producers)
                positions[i] += rng.rand() * (positions[j] - positions[i])
            else:
                # Move towards global best
                positions[i] += rng.rand() * (best_pos - positions[i])

        # Danger awareness  random sparrows move away
        n_danger = max(1, N_SPARROWS // 10)
        for _ in range(n_danger):
            idx = rng.randint(0, N_SPARROWS)
            positions[idx] = best_pos + rng.randn(n_dim) * 0.05

        # Clip to [0, 1]
        positions = np.clip(positions, 0, 1)

        elapsed = time.time() - t0
        print(f"   Iter {it+1:2d}/{N_ITER_SSA} | Best AP: {best_fitness:.6f} | "
              f"Stale: {stale_count}/{SSA_PATIENCE} | Time: {elapsed:.0f}s")

        if stale_count >= SSA_PATIENCE:
            print(f"\n   Early stop: no improvement in {SSA_PATIENCE} iterations.")
            break

    best_params = decode_sparrow(best_pos)
    print(f"\n SSA finished in {(time.time()-t0)/60:.1f} min ({it+1} iterations used)")
    print(f"   Best Average Precision: {best_fitness:.6f}")
    print(f"   Best params: {json.dumps(best_params, indent=2)}")

    return best_params, convergence


# 
# 5. FINAL TRAINING + EVALUATION
# 
def train_final_model(best_params, X_train, y_train, X_test, y_test):
    """Train final model with best SSA params on full balanced train, evaluate on full test."""
    print("\n Training final model with best params...")

    best_model = xgb.XGBClassifier(
        **best_params,
        n_estimators=1000,
        early_stopping_rounds=50,
        eval_metric="aucpr",
        use_label_encoder=False,
        enable_categorical=True,
        verbosity=0,
        random_state=RANDOM_STATE,
        tree_method="hist"
    )

    print("\n   Fitting final model (early stop if no gain in 50 rounds)...")
    best_model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        verbose=False,
    )
    print(f"   Best iteration: {best_model.best_iteration}")

    y_prob = best_model.predict_proba(X_test)[:, 1]
    return best_model, y_prob


def calibrate_threshold(y_test, y_prob):
    """Find optimal thresholds for F1 and F2."""
    precisions, recalls, thresholds = precision_recall_curve(y_test, y_prob)

    # F1 optimal
    f1_scores = 2 * precisions * recalls / (precisions + recalls + 1e-8)
    idx_f1    = np.argmax(f1_scores)
    best_f1_thresh = thresholds[min(idx_f1, len(thresholds) - 1)]

    # F2 optimal (beta=2, favors recall)
    beta = 2
    f2_scores = (1 + beta**2) * precisions * recalls / (beta**2 * precisions + recalls + 1e-8)
    idx_f2    = np.argmax(f2_scores)
    best_f2_thresh = thresholds[min(idx_f2, len(thresholds) - 1)]

    return best_f1_thresh, best_f2_thresh, precisions, recalls, thresholds


def compute_metrics_at_threshold(y_test, y_prob, threshold, label=""):
    """Compute and print confusion matrix metrics at a given threshold."""
    y_pred = (y_prob >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_test, y_pred).ravel()
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1        = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    print(f"\n Metrics @ threshold={threshold:.3f} {label}")
    print(f"   TP={tp:,}  FP={fp:,}  FN={fn:,}  TN={tn:,}")
    print(f"   Precision: {precision:.4f}  Recall: {recall:.4f}  F1: {f1:.4f}")
    print(f"   Total alerts: {tp+fp:,}")

    return {
        "threshold": threshold, "precision": precision, "recall": recall,
        "f1": f1, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "alerts": tp + fp,
    }


# 
# 6. VISUALIZATION
# 
def plot_evaluation(y_test, y_prob, convergence, feature_names, model, out_dir):
    """Generate evaluation plots."""
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("Model V2  Evaluation", fontsize=16, fontweight="bold")

    # 1. ROC Curve
    fpr, tpr, _ = roc_curve(y_test, y_prob)
    auc = roc_auc_score(y_test, y_prob)
    axes[0, 0].plot(fpr, tpr, color="#E8612A", lw=2, label=f"AUC = {auc:.4f}")
    axes[0, 0].plot([0, 1], [0, 1], "k--", alpha=0.3)
    axes[0, 0].set_title("ROC Curve")
    axes[0, 0].set_xlabel("False Positive Rate")
    axes[0, 0].set_ylabel("True Positive Rate")
    axes[0, 0].legend()
    axes[0, 0].grid(alpha=0.3)

    # 2. Precision-Recall Curve
    pr, rc, _ = precision_recall_curve(y_test, y_prob)
    ap = average_precision_score(y_test, y_prob)
    axes[0, 1].plot(rc, pr, color="#00897B", lw=2, label=f"AP = {ap:.4f}")
    axes[0, 1].set_title("Precision-Recall Curve")
    axes[0, 1].set_xlabel("Recall")
    axes[0, 1].set_ylabel("Precision")
    axes[0, 1].legend()
    axes[0, 1].grid(alpha=0.3)

    # 3. SSA Convergence
    axes[1, 0].plot(range(1, len(convergence) + 1), convergence,
                     "o-", color="#1565C0", lw=2, markersize=5)
    axes[1, 0].set_title("SSA Convergence")
    axes[1, 0].set_xlabel("Iteration")
    axes[1, 0].set_ylabel("Best Average Precision (CV)")
    axes[1, 0].grid(alpha=0.3)

    # 4. Feature Importance (top 15)
    imp = model.feature_importances_
    idx = np.argsort(imp)[-15:]
    axes[1, 1].barh(
        [feature_names[i] for i in idx], imp[idx],
        color="#E8612A", alpha=0.85
    )
    axes[1, 1].set_title("Top 15 Feature Importances")
    axes[1, 1].grid(alpha=0.3, axis="x")

    plt.tight_layout()
    path = out_dir / "evaluation_v2.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"\n Evaluation plot saved: {path}")


# 
# MAIN
# 
def main():
    print("=" * 70)
    print("   AlertaFuego  Model V2 Training Pipeline")
    print("=" * 70)
    t_start = time.time()

    # 1. Load
    df = load_data()

    # 2. Feature engineering
    df = add_features(df)

    # 3. Determine feature columns (everything except IDs)
    feature_cols = [c for c in df.columns if c not in ID_COLS]
    print(f"   Using {len(feature_cols)} features")

    # 4. Temporal split + balanced sampling
    df_train, df_test = split_and_sample(df)
    X_train, y_train  = prepare_xy(df_train, feature_cols)
    X_test,  y_test   = prepare_xy(df_test, feature_cols)

    print(f"\n   Train shape: {X_train.shape}  |  Test shape: {X_test.shape}")
    print(f"   Test fire rate: {y_test.mean()*100:.2f}%")

    # 5. SSA optimization
    best_params, convergence = ssa_optimize(X_train, y_train)

    # 6. Final training
    model, y_prob = train_final_model(best_params, X_train, y_train, X_test, y_test)

    # 7. Metrics
    auc  = roc_auc_score(y_test, y_prob)
    ap   = average_precision_score(y_test, y_prob)
    print(f"\n Global Metrics on FULL test set:")
    print(f"   AUC-ROC:  {auc:.4f}")
    print(f"   Avg Prec: {ap:.4f}")

    # 8. Threshold calibration
    best_f1_thresh, best_f2_thresh, _, _, _ = calibrate_threshold(y_test, y_prob)

    metrics_f1 = compute_metrics_at_threshold(y_test, y_prob, best_f1_thresh, "(F1-optimal)")
    metrics_f2 = compute_metrics_at_threshold(y_test, y_prob, best_f2_thresh, "(F2-optimal, recall-biased)")

    # Also evaluate at fixed thresholds for comparison with v1
    thresholds_table = []
    for t in [0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8]:
        m = compute_metrics_at_threshold(y_test, y_prob, t)
        thresholds_table.append(m)

    # 9. Save outputs
    # Best params
    with open(OUT_DIR / "best_params_v2.json", "w") as f:
        json.dump(best_params, f, indent=2)

    # Metrics summary
    metrics_summary = {
        "auc_roc": auc,
        "avg_precision": ap,
        "best_f1_threshold": best_f1_thresh,
        "best_f2_threshold": best_f2_thresh,
        "precision_at_f1": metrics_f1["precision"],
        "recall_at_f1": metrics_f1["recall"],
        "f1_at_f1": metrics_f1["f1"],
        "fp_at_f1": metrics_f1["fp"],
        "precision_at_f2": metrics_f2["precision"],
        "recall_at_f2": metrics_f2["recall"],
        "n_sparrows": N_SPARROWS,
        "n_iter": N_ITER_SSA,
        "train_size": len(df_train),
        "test_size": len(df_test),
    }
    pd.DataFrame([metrics_summary]).T.to_csv(OUT_DIR / "metricas_v2.csv", header=["valor"])

    # Convergence
    pd.DataFrame({"best_ap": convergence}).to_csv(OUT_DIR / "ssa_convergence_v2.csv")

    # Threshold calibration table
    pd.DataFrame(thresholds_table).to_csv(OUT_DIR / "threshold_calibration_v2.csv", index=False)

    # Feature importance
    fi = pd.DataFrame({
        "feature": feature_cols,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)
    fi.to_csv(OUT_DIR / "feature_importance_v2.csv", index=False)

    # Save model
    model.save_model(str(OUT_DIR / "xgboost_v2.json"))

    # 10. Plots
    plot_evaluation(y_test, y_prob, convergence, feature_cols, model, OUT_DIR)

    # 11. Comparison with v1
    print("\n" + "=" * 70)
    print("   V1 vs V2 Comparison")
    print("=" * 70)
    print(f"  {'Metric':<25} {'V1':>12} {'V2':>12} {'Delta':>12}")
    print(f"  {'-'*25} {'-'*12} {'-'*12} {'-'*12}")

    v1_auc  = 0.8634
    v1_prec = 0.0305
    v1_rec  = 0.9653
    v1_fp   = 515123

    print(f"  {'AUC-ROC':<25} {v1_auc:>12.4f} {auc:>12.4f} {auc-v1_auc:>+12.4f}")
    print(f"  {'Avg Precision':<25} {0.1814:>12.4f} {ap:>12.4f} {ap-0.1814:>+12.4f}")
    print(f"  {'Precision (F1-opt)':<25} {v1_prec:>12.4f} {metrics_f1['precision']:>12.4f} "
          f"{metrics_f1['precision']-v1_prec:>+12.4f}")
    print(f"  {'Recall (F1-opt)':<25} {v1_rec:>12.4f} {metrics_f1['recall']:>12.4f} "
          f"{metrics_f1['recall']-v1_rec:>+12.4f}")
    print(f"  {'False Positives':<25} {v1_fp:>12,} {metrics_f1['fp']:>12,} "
          f"{metrics_f1['fp']-v1_fp:>+12,}")

    total_time = (time.time() - t_start) / 60
    print(f"\n  Total pipeline time: {total_time:.1f} minutes")
    print(f" All outputs saved to: {OUT_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    main()

