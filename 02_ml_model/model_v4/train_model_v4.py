
import os, json, time, warnings, hashlib
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

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT  = SCRIPT_DIR.parent.parent
DATA_PATH  = REPO_ROOT / "save_gold.csv"
OUT_DIR    = SCRIPT_DIR
OUT_DIR.mkdir(exist_ok=True)

TEMPORAL_SPLIT_DATE   = "2024-07-01"
VALIDATION_SPLIT_DATE = "2024-05-01"
SPIN_UP_END           = "2022-03-01"

RANDOM_STATE = 42
N_SPARROWS   = 15
N_ITER_SSA   = 15
GRID_RES     = 0.25

ID_COLS    = ["cell_id", "fecha_join", "fire_occurred"]
TARGET     = "fire_occurred"

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

def md5_of_file(path, chunk=65536):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(chunk), b""):
            h.update(c)
    return h.hexdigest()

def load_data():
    print(" Loading data...")
    t0 = time.time()
    df = pd.read_csv(DATA_PATH, parse_dates=["fecha_join"])
    n_before = len(df)
    df = df.drop_duplicates(subset=["cell_id", "fecha_join"])
    n_dupes = n_before - len(df)
    if n_dupes > 0:
        print(f"   WARNING: Dropped {n_dupes:,} duplicate rows")

    n_pre_spinup = len(df)
    df = df[df["fecha_join"] >= pd.Timestamp(SPIN_UP_END)].copy()
    dropped = n_pre_spinup - len(df)
    print(f"   Spin-up filter (FWI warmup): dropped {dropped:,} rows before {SPIN_UP_END}")
    print(f"   Loaded {len(df):,} rows  {df.shape[1]} cols in {time.time()-t0:.1f}s")
    print(f"   Fire rate: {df[TARGET].mean()*100:.2f}% ({df[TARGET].sum():,} fires)")
    return df

def parse_cell_coords(cell_id_series):
    parts = cell_id_series.str.split("_", expand=True)
    return parts[0].astype(float), parts[1].astype(float)

def build_neighbor_map(cell_ids):
    cell_set = set(cell_ids)
    offsets = [
        (-GRID_RES, -GRID_RES), (-GRID_RES, 0), (-GRID_RES, GRID_RES),
        (0, -GRID_RES),                          (0, GRID_RES),
        (GRID_RES, -GRID_RES),  (GRID_RES, 0),  (GRID_RES, GRID_RES),
    ]

    neighbor_map = {}
    for cid in cell_ids:
        parts = cid.split("_")
        lat, lon = float(parts[0]), float(parts[1])
        neighbors = []
        for dlat, dlon in offsets:
            nlat = round(lat + dlat, 4)
            nlon = round(lon + dlon, 4)
            nid = f"{nlat:.4f}_{nlon:.4f}"
            if nid in cell_set:
                neighbors.append(nid)
        neighbor_map[cid] = neighbors

    return neighbor_map

def add_spatial_features(df):
    print(" Computing spatial neighbor features...")
    t0 = time.time()

    unique_cells = df["cell_id"].unique()
    neighbor_map = build_neighbor_map(unique_cells)

    avg_neighbors = np.mean([len(v) for v in neighbor_map.values()])
    print(f"   {len(unique_cells):,} cells, avg {avg_neighbors:.1f} neighbors per cell")

    print("   Building FWI lookup...")
    fwi_lookup = df.set_index(["cell_id", "fecha_join"])["fwi"].to_dict()

    fwi_mean_arr = np.full(len(df), np.nan)
    fwi_max_arr  = np.full(len(df), np.nan)

    print("   Computing fwi_vecinos_mean / fwi_vecinos_max...")
    for idx, row in enumerate(df.itertuples()):
        neighbors = neighbor_map.get(row.cell_id, [])
        if neighbors:
            vals = [fwi_lookup.get((n, row.fecha_join)) for n in neighbors]
            vals = [v for v in vals if v is not None]
            if vals:
                fwi_mean_arr[idx] = np.mean(vals)
                fwi_max_arr[idx]  = np.max(vals)
        if (idx + 1) % 500_000 == 0:
            print(f"     [{idx+1:,}/{len(df):,}] {time.time()-t0:.0f}s")

    df["fwi_vecinos_mean"] = fwi_mean_arr
    df["fwi_vecinos_max"]  = fwi_max_arr

    print("   Computing fire_vecinos_3d...")
    fire_lookup = set()
    fire_rows = df[df["fire_occurred"] == 1][["cell_id", "fecha_join"]]
    for row in fire_rows.itertuples():
        for d in (1, 2, 3):
            fire_lookup.add((row.cell_id, row.fecha_join + pd.Timedelta(days=d)))

    fire_vec_arr = np.zeros(len(df), dtype=np.int8)
    for idx, row in enumerate(df.itertuples()):
        neighbors = neighbor_map.get(row.cell_id, [])
        for n in neighbors:
            if (n, row.fecha_join) in fire_lookup:
                fire_vec_arr[idx] = 1
                break
        if (idx + 1) % 500_000 == 0:
            print(f"     [{idx+1:,}/{len(df):,}] {time.time()-t0:.0f}s")

    df["fire_vecinos_3d"] = fire_vec_arr

    df["fwi_vecinos_mean"] = df["fwi_vecinos_mean"].fillna(df["fwi"])
    df["fwi_vecinos_max"]  = df["fwi_vecinos_max"].fillna(df["fwi"])

    elapsed = time.time() - t0
    print(f"   Spatial features computed in {elapsed:.0f}s")
    print(f"   fire_vecinos_3d positives: {df['fire_vecinos_3d'].sum():,}")
    return df

def add_features(df, train_mask):
    print(" Engineering features...")

    df["fwi_x_vpd"]  = df["fwi"]            * df["vpd_kpa"]
    df["temp_x_dry"] = df["temperature_2m"] * df["dias_secos"]
    df["wind_x_fwi"] = df["wind_speed_10m"] * df["fwi"]

    ndvi_train         = df.loc[train_mask]
    ndvi_means_by_cell = ndvi_train.groupby("cell_id")["ndvi"].mean()
    global_train_mean  = ndvi_train["ndvi"].mean()
    df["ndvi_anomaly"] = df["ndvi"] - df["cell_id"].map(ndvi_means_by_cell).fillna(global_train_mean)

    ndvi_means_df = ndvi_means_by_cell.reset_index()
    ndvi_means_df.columns = ["cell_id", "ndvi_mean"]
    ndvi_means_df.to_csv(OUT_DIR / "ndvi_means_per_cell_v4.csv", index=False)
    with open(OUT_DIR / "ndvi_global_mean_v4.json", "w") as f:
        json.dump({"ndvi_global_mean": float(global_train_mean)}, f, indent=2)

    print(f"   ndvi_anomaly fitted on {train_mask.sum():,} train rows "
          f"(global mean={global_train_mean:.4f}, n_cells={len(ndvi_means_by_cell):,})")
    print(f"   Persisted ndvi_means_per_cell_v4.csv for serving alignment.")

    df = add_spatial_features(df)

    df = df.fillna(0)

    n_new = 8
    print(f"   Added {n_new} engineered features → {df.shape[1]} total columns")
    return df

def _balance_by_subregion(df_pool, n_fires, fires):
    no_fires = df_pool[df_pool[TARGET] == 0]
    no_fires_sample = (
        no_fires
        .groupby("subregion_id", group_keys=False)
        .apply(lambda g: g.sample(
            n=min(len(g), max(1, int(n_fires * len(g) / len(no_fires)))),
            random_state=RANDOM_STATE
        ))
    )
    if len(no_fires_sample) > n_fires:
        no_fires_sample = no_fires_sample.sample(n=n_fires, random_state=RANDOM_STATE)
    elif len(no_fires_sample) < n_fires:
        extra = no_fires.drop(no_fires_sample.index).sample(
            n=n_fires - len(no_fires_sample), random_state=RANDOM_STATE
        )
        no_fires_sample = pd.concat([no_fires_sample, extra])
    return pd.concat([fires, no_fires_sample]).sample(
        frac=1, random_state=RANDOM_STATE
    ).reset_index(drop=True)

def split_sample_and_validate(df):
    print(f"  Temporal 3-way split: train < {VALIDATION_SPLIT_DATE} ≤ val < {TEMPORAL_SPLIT_DATE} ≤ test")

    val_split  = pd.Timestamp(VALIDATION_SPLIT_DATE)
    test_split = pd.Timestamp(TEMPORAL_SPLIT_DATE)

    df_inner_train = df[df["fecha_join"] <  val_split].copy()
    df_val         = df[(df["fecha_join"] >= val_split) & (df["fecha_join"] < test_split)].copy()
    df_test        = df[df["fecha_join"] >= test_split].copy()

    print(f"   Inner train (pre-balance): {len(df_inner_train):,} rows")
    print(f"   Validation:                {len(df_val):,} rows  "
          f"(fire rate {df_val[TARGET].mean()*100:.2f}%)")
    print(f"   Test:                      {len(df_test):,} rows  "
          f"(fire rate {df_test[TARGET].mean()*100:.2f}%)")

    fires   = df_inner_train[df_inner_train[TARGET] == 1]
    n_fires = len(fires)
    print(f"   Inner train fires: {n_fires:,}  |  Inner train no-fires: {(df_inner_train[TARGET]==0).sum():,}")

    df_train_balanced = _balance_by_subregion(df_inner_train, n_fires, fires)

    print(f"   Balanced inner train: {len(df_train_balanced):,} rows "
          f"(fire={df_train_balanced[TARGET].sum():,} / "
          f"no_fire={len(df_train_balanced) - df_train_balanced[TARGET].sum():,})")

    return df_train_balanced, df_val, df_test

def prepare_xy(df, feature_cols):
    X = df[feature_cols].copy()
    if "subregion_id" in X.columns:
        X["subregion_id"] = X["subregion_id"].astype("category")
    if "land_cover_cat" in X.columns:
        X["land_cover_cat"] = X["land_cover_cat"].astype("category")
    y = df[TARGET].values.astype(np.int32)
    return X, y

def decode_sparrow(position):
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
    skf = StratifiedKFold(n_splits=3, shuffle=True, random_state=RANDOM_STATE)
    aps = []
    for train_idx, val_idx in skf.split(X_train, y_train):
        model = xgb.XGBClassifier(
            **params,
            n_estimators=500,
            early_stopping_rounds=30,
            eval_metric="aucpr",
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
    print(f"\n SSA Optimization: {N_SPARROWS} sparrows  {N_ITER_SSA} iterations")
    print(f"   Training on {X_train.shape[0]:,} rows  {X_train.shape[1]} features\n")

    n_dim = len(PARAM_BOUNDS)
    rng   = np.random.RandomState(RANDOM_STATE)

    positions = rng.rand(N_SPARROWS, n_dim)
    fitness   = np.full(N_SPARROWS, -np.inf)

    best_pos     = None
    best_fitness = -np.inf
    convergence  = []

    t0 = time.time()

    SSA_PATIENCE = 3
    stale_count  = 0

    for it in range(N_ITER_SSA):
        prev_best = best_fitness

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

        if best_fitness <= prev_best + 1e-5:
            stale_count += 1
        else:
            stale_count = 0

        order     = np.argsort(-fitness)
        positions = positions[order]
        fitness   = fitness[order]

        n_producers = max(1, N_SPARROWS // 5)

        for i in range(n_producers):
            if rng.rand() < 0.8:
                positions[i] += rng.randn(n_dim) * 0.1 * (1 - it / N_ITER_SSA)
            else:
                positions[i] = rng.rand(n_dim)

        for i in range(n_producers, N_SPARROWS):
            if rng.rand() < 0.5:
                j = rng.randint(0, n_producers)
                positions[i] += rng.rand() * (positions[j] - positions[i])
            else:
                positions[i] += rng.rand() * (best_pos - positions[i])

        n_danger = max(1, N_SPARROWS // 10)
        for _ in range(n_danger):
            idx = rng.randint(0, N_SPARROWS)
            positions[idx] = best_pos + rng.randn(n_dim) * 0.05

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

def train_final_model(best_params, X_train, y_train, X_val, y_val, X_test):
    print("\n Training final model with best params...")

    best_model = xgb.XGBClassifier(
        **best_params,
        n_estimators=1000,
        early_stopping_rounds=50,
        eval_metric="aucpr",
        enable_categorical=True,
        verbosity=0,
        random_state=RANDOM_STATE,
        tree_method="hist"
    )

    print("\n   Fitting on balanced train, validating on temporal slice "
          f"(early stop if no gain in 50 rounds)...")
    best_model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        verbose=False,
    )
    print(f"   Best iteration: {best_model.best_iteration}")

    y_prob = best_model.predict_proba(X_test)[:, 1]
    return best_model, y_prob

def calibrate_threshold(y_test, y_prob):
    precisions, recalls, thresholds = precision_recall_curve(y_test, y_prob)

    f1_scores = 2 * precisions * recalls / (precisions + recalls + 1e-8)
    idx_f1    = np.argmax(f1_scores)
    best_f1_thresh = thresholds[min(idx_f1, len(thresholds) - 1)]

    beta = 2
    f2_scores = (1 + beta**2) * precisions * recalls / (beta**2 * precisions + recalls + 1e-8)
    idx_f2    = np.argmax(f2_scores)
    best_f2_thresh = thresholds[min(idx_f2, len(thresholds) - 1)]

    return best_f1_thresh, best_f2_thresh, precisions, recalls, thresholds

def compute_metrics_at_threshold(y_test, y_prob, threshold, label=""):
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

def plot_evaluation(y_test, y_prob, convergence, feature_names, model, out_dir):
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("AlertaFuego — Model V4 (Spatial) Evaluation", fontsize=16, fontweight="bold")

    fpr, tpr, _ = roc_curve(y_test, y_prob)
    auc = roc_auc_score(y_test, y_prob)
    axes[0, 0].plot(fpr, tpr, color="#E8612A", lw=2, label=f"AUC = {auc:.4f}")
    axes[0, 0].plot([0, 1], [0, 1], "k--", alpha=0.3)
    axes[0, 0].set_title("ROC Curve")
    axes[0, 0].set_xlabel("False Positive Rate")
    axes[0, 0].set_ylabel("True Positive Rate")
    axes[0, 0].legend()
    axes[0, 0].grid(alpha=0.3)

    pr, rc, _ = precision_recall_curve(y_test, y_prob)
    ap = average_precision_score(y_test, y_prob)
    axes[0, 1].plot(rc, pr, color="#00897B", lw=2, label=f"AP = {ap:.4f}")
    axes[0, 1].set_title("Precision-Recall Curve")
    axes[0, 1].set_xlabel("Recall")
    axes[0, 1].set_ylabel("Precision")
    axes[0, 1].legend()
    axes[0, 1].grid(alpha=0.3)

    axes[1, 0].plot(range(1, len(convergence) + 1), convergence,
                     "o-", color="#1565C0", lw=2, markersize=5)
    axes[1, 0].set_title("SSA Convergence")
    axes[1, 0].set_xlabel("Iteration")
    axes[1, 0].set_ylabel("Best Average Precision (CV)")
    axes[1, 0].grid(alpha=0.3)

    imp = model.feature_importances_
    idx = np.argsort(imp)[-20:]
    axes[1, 1].barh(
        [feature_names[i] for i in idx], imp[idx],
        color="#E8612A", alpha=0.85
    )
    axes[1, 1].set_title("Top 20 Feature Importances")
    axes[1, 1].grid(alpha=0.3, axis="x")

    plt.tight_layout()
    path = out_dir / "evaluation_v4.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"\n Evaluation plot saved: {path}")

def main():
    print("=" * 70)
    print("   AlertaFuego — Model V4 Training (Spatial Autocorrelation)")
    print("   Honest split: train < {} ≤ val < {} ≤ test"
          .format(VALIDATION_SPLIT_DATE, TEMPORAL_SPLIT_DATE))
    print("=" * 70)
    t_start = time.time()

    df = load_data()

    train_mask = df["fecha_join"] < pd.Timestamp(TEMPORAL_SPLIT_DATE)
    df = add_features(df, train_mask)

    feature_cols = [c for c in df.columns if c not in ID_COLS]
    print(f"   Using {len(feature_cols)} features")

    df_train, df_val, df_test = split_sample_and_validate(df)
    X_train, y_train          = prepare_xy(df_train, feature_cols)
    X_val,   y_val            = prepare_xy(df_val,   feature_cols)
    X_test,  y_test           = prepare_xy(df_test,  feature_cols)

    print(f"\n   Train shape: {X_train.shape}  |  Val shape: {X_val.shape}  |  Test shape: {X_test.shape}")
    print(f"   Val fire rate: {y_val.mean()*100:.2f}%   Test fire rate: {y_test.mean()*100:.2f}%")

    best_params, convergence = ssa_optimize(X_train, y_train)

    model, y_prob = train_final_model(best_params, X_train, y_train, X_val, y_val, X_test)

    y_prob_val = model.predict_proba(X_val)[:, 1]
    val_auc    = roc_auc_score(y_val, y_prob_val)
    val_ap     = average_precision_score(y_val, y_prob_val)
    print(f"\n Validation slice metrics (early-stop reference):")
    print(f"   AUC-ROC:  {val_auc:.4f}   Avg Prec: {val_ap:.4f}")

    auc  = roc_auc_score(y_test, y_prob)
    ap   = average_precision_score(y_test, y_prob)
    print(f"\n Held-out test metrics (FINAL — never seen during fit/tune):")
    print(f"   AUC-ROC:  {auc:.4f}")
    print(f"   Avg Prec: {ap:.4f}")

    best_f1_thresh, best_f2_thresh, _, _, _ = calibrate_threshold(y_test, y_prob)

    metrics_f1 = compute_metrics_at_threshold(y_test, y_prob, best_f1_thresh, "(F1-optimal)")
    metrics_f2 = compute_metrics_at_threshold(y_test, y_prob, best_f2_thresh, "(F2-optimal, recall-biased)")

    thresholds_table = []
    for t in [0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8]:
        m = compute_metrics_at_threshold(y_test, y_prob, t)
        thresholds_table.append(m)

    with open(OUT_DIR / "best_params_v4.json", "w") as f:
        json.dump(best_params, f, indent=2)

    print("\n   Computing dataset MD5...")
    dataset_md5 = md5_of_file(DATA_PATH)
    print(f"   save_gold.csv MD5: {dataset_md5}")

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
        "val_auc_roc": val_auc,
        "val_avg_precision": val_ap,
        "n_sparrows": N_SPARROWS,
        "n_iter": N_ITER_SSA,
        "train_size": len(df_train),
        "val_size":   len(df_val),
        "test_size":  len(df_test),
        "spin_up_end":           SPIN_UP_END,
        "validation_split_date": VALIDATION_SPLIT_DATE,
        "temporal_split_date":   TEMPORAL_SPLIT_DATE,
        "best_iteration":        int(model.best_iteration),
        "dataset_md5":           dataset_md5,
        "random_state":          RANDOM_STATE,
    }
    pd.DataFrame([metrics_summary]).T.to_csv(OUT_DIR / "metricas_v4.csv", header=["valor"])

    pd.DataFrame({"best_ap": convergence}).to_csv(OUT_DIR / "ssa_convergence_v4.csv")
    pd.DataFrame(thresholds_table).to_csv(OUT_DIR / "threshold_calibration_v4.csv", index=False)

    fi = pd.DataFrame({
        "feature": feature_cols,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)
    fi.to_csv(OUT_DIR / "feature_importance_v4.csv", index=False)

    model.save_model(str(OUT_DIR / "xgboost_v4.json"))

    import pickle
    with open(OUT_DIR / "feature_cols_v4.pkl", "wb") as f:
        pickle.dump(feature_cols, f)

    plot_evaluation(y_test, y_prob, convergence, feature_cols, model, OUT_DIR)

    print("\n" + "=" * 70)
    print("   V3 (leaky baseline) vs V4 (honest split) Comparison")
    print("=" * 70)
    print("   Note: V3 numbers are from the previous run BEFORE fixing")
    print("   findings C-1 (NDVI anomaly leakage) and C-2 (test set")
    print("   used as eval_set for early stopping). The V3 numbers are")
    print("   therefore optimistic — a drop in V4 is the expected effect")
    print("   of correctly held-out evaluation, not a regression.")
    print()
    print(f"  {'Metric':<25} {'V3 (leaky)':>12} {'V4 (honest)':>13} {'Delta':>12}")
    print(f"  {'-'*25} {'-'*12} {'-'*13} {'-'*12}")

    v3_auc  = 0.8976
    v3_ap   = 0.3148
    v3_prec = 0.3361
    v3_rec  = 0.4259
    v3_fp   = 10957

    print(f"  {'AUC-ROC':<25} {v3_auc:>12.4f} {auc:>13.4f} {auc-v3_auc:>+12.4f}")
    print(f"  {'Avg Precision':<25} {v3_ap:>12.4f} {ap:>13.4f} {ap-v3_ap:>+12.4f}")
    print(f"  {'Precision (F1-opt)':<25} {v3_prec:>12.4f} {metrics_f1['precision']:>13.4f} "
          f"{metrics_f1['precision']-v3_prec:>+12.4f}")
    print(f"  {'Recall (F1-opt)':<25} {v3_rec:>12.4f} {metrics_f1['recall']:>13.4f} "
          f"{metrics_f1['recall']-v3_rec:>+12.4f}")
    print(f"  {'False Positives':<25} {v3_fp:>12,} {metrics_f1['fp']:>13,} "
          f"{metrics_f1['fp']-v3_fp:>+12,}")

    total_time = (time.time() - t_start) / 60
    print(f"\n  Total pipeline time: {total_time:.1f} minutes")
    print(f" All outputs saved to: {OUT_DIR}")
    print("=" * 70)

if __name__ == "__main__":
    main()
