
import os, json, time, warnings
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

from sklearn.metrics import (
    roc_auc_score, average_precision_score, precision_recall_curve,
)
from sklearn.calibration import calibration_curve
import xgboost as xgb

warnings.filterwarnings("ignore")

import train_model_v4 as t4

SCRIPT_DIR = Path(__file__).resolve().parent
OUT_DIR    = SCRIPT_DIR / "eval"
OUT_DIR.mkdir(exist_ok=True)

RS = t4.RANDOM_STATE

FEATURE_BLOCKS = {
    "spatial":      ["fire_vecinos_3d", "fwi_vecinos_mean", "fwi_vecinos_max"],
    "fwi_system":   ["ffmc", "dmc", "isi", "bui", "fwi", "fwi_roll14", "fwi_roll30"],
    "weather":      ["temperature_2m", "relative_humidity", "precipitation",
                     "wind_speed_10m", "vpd_kpa", "solar_radiation",
                     "soil_moisture_0_7cm", "soil_moisture_28_100cm",
                     "temperature_2m_roll30", "wind_speed_10m_roll30",
                     "dias_secos", "spi_90d"],
    "vegetation":   ["ndvi", "ndvi_anomaly"],
    "static":       ["elevation", "slope", "aspect", "dist_road_km",
                     "pop_density_km2", "land_cover_cat", "subregion_id"],
    "seasonality":  ["mes_sin", "mes_cos", "dia_sin", "dia_cos",
                     "calendario_agricola"],
    "interactions": ["fwi_x_vpd", "temp_x_dry", "wind_x_fwi"],
}

CAT_COLS = ["subregion_id", "land_cover_cat"]

def _xgb(params):
    return xgb.XGBClassifier(
        **params,
        n_estimators=1000,
        early_stopping_rounds=50,
        eval_metric="aucpr",
        enable_categorical=True,
        verbosity=0,
        random_state=RS,
        tree_method="hist",
    )

def _prep(df, cols):
    X = df[cols].copy()
    for c in CAT_COLS:
        if c in X.columns:
            X[c] = X[c].astype("category")
    y = df[t4.TARGET].values.astype(np.int32)
    return X, y

def main():
    t_start = time.time()
    report = {}
    print("=" * 70)
    print("   AlertaFuego — Model V4 Rigorous Evaluation (Phase A)")
    print("=" * 70)

    df = t4.load_data()
    train_mask = df["fecha_join"] < pd.Timestamp(t4.TEMPORAL_SPLIT_DATE)
    df = t4.add_features(df, train_mask)

    import pickle
    with open(SCRIPT_DIR / "feature_cols_v4.pkl", "rb") as f:
        feature_cols = pickle.load(f)
    print(f"\n   Loaded {len(feature_cols)} feature columns from feature_cols_v4.pkl")

    df_train, df_val, df_test = t4.split_sample_and_validate(df)
    X_train, y_train = _prep(df_train, feature_cols)
    X_val,   y_val   = _prep(df_val,   feature_cols)
    X_test,  y_test  = _prep(df_test,  feature_cols)

    best_params = json.load(open(SCRIPT_DIR / "best_params_v4.json"))

    model = xgb.XGBClassifier(enable_categorical=True)
    model.load_model(str(SCRIPT_DIR / "xgboost_v4.json"))
    y_prob = model.predict_proba(X_test)[:, 1]

    auc_full = roc_auc_score(y_test, y_prob)
    ap_full  = average_precision_score(y_test, y_prob)
    prevalence = float(y_test.mean())

    expected_auc = 0.8965344780215139
    print("\n" + "─" * 70)
    print(f"   REPRODUCIBILITY CHECK")
    print(f"   Reproduced test AUC: {auc_full:.6f}  (expected {expected_auc:.6f})")
    drift = abs(auc_full - expected_auc)
    if drift < 1e-3:
        print(f"   OK — matches metricas_v4.csv (drift {drift:.2e})")
    else:
        print(f"   !! WARNING — drift {drift:.2e}. Split/feature reproduction "
              f"diverged; treat the rest with caution.")
    print("─" * 70)

    report["repro"] = {"auc_full": auc_full, "ap_full": ap_full,
                       "expected_auc": expected_auc, "drift": drift,
                       "test_prevalence": prevalence, "test_size": int(len(y_test))}

    print("\n" + "=" * 70)
    print("   T1. fire_vecinos_3d serving-degradation analysis")
    print("=" * 70)
    horizon_rows = []

    horizon_rows.append({"scenario": "full_features_H1", "auc": auc_full, "ap": ap_full,
                         "note": "fire_vecinos_3d observed (forecast day 1 / hindcast)"})

    X_test_masked = X_test.copy()
    X_test_masked["fire_vecinos_3d"] = 0
    y_prob_masked = model.predict_proba(X_test_masked)[:, 1]
    auc_masked = roc_auc_score(y_test, y_prob_masked)
    ap_masked  = average_precision_score(y_test, y_prob_masked)
    horizon_rows.append({"scenario": "fire_vec_masked0_currentmodel", "auc": auc_masked,
                         "ap": ap_masked,
                         "note": "deployed model fed 0 (forecast day >=4)"})
    print(f"   Full (H=1)            : AUC {auc_full:.4f}  AP {ap_full:.4f}")
    print(f"   Masked 0 (H>=4, same model): AUC {auc_masked:.4f}  AP {ap_masked:.4f}"
          f"   Δ AUC {auc_masked-auc_full:+.4f}  Δ AP {ap_masked-ap_full:+.4f}")

    cols_no_fv = [c for c in feature_cols if c != "fire_vecinos_3d"]
    m_no_fv = _xgb(best_params)
    m_no_fv.fit(X_train[cols_no_fv], y_train,
                eval_set=[(X_val[cols_no_fv], y_val)], verbose=False)
    p_no_fv = m_no_fv.predict_proba(X_test[cols_no_fv])[:, 1]
    auc_no_fv = roc_auc_score(y_test, p_no_fv)
    ap_no_fv  = average_precision_score(y_test, p_no_fv)
    horizon_rows.append({"scenario": "retrained_without_fire_vec", "auc": auc_no_fv,
                         "ap": ap_no_fv,
                         "note": "honest multi-day ceiling (model designed w/o the feature)"})
    print(f"   Retrained w/o fire_vec: AUC {auc_no_fv:.4f}  AP {ap_no_fv:.4f}"
          f"   Δ AUC {auc_no_fv-auc_full:+.4f}  Δ AP {ap_no_fv-ap_full:+.4f}")

    pd.DataFrame(horizon_rows).to_csv(OUT_DIR / "horizon_degradation_v4.csv", index=False)
    report["T1_horizon"] = horizon_rows

    print("\n" + "=" * 70)
    print("   D1. Baselines")
    print("=" * 70)
    baselines = []

    baselines.append({"baseline": "prevalence_constant", "auc": 0.5, "ap": prevalence,
                      "note": f"always predict base rate {prevalence:.4f}"})

    fire_rows = df[df[t4.TARGET] == 1][["cell_id", "fecha_join"]]
    self_lookup = set()
    for r in fire_rows.itertuples():
        for d in (1, 2, 3):
            self_lookup.add((r.cell_id, r.fecha_join + pd.Timedelta(days=d)))
    persist_score = np.array([
        1.0 if (c, pd.Timestamp(f)) in self_lookup else 0.0
        for c, f in zip(df_test["cell_id"].values, df_test["fecha_join"].values)
    ])
    auc_persist = roc_auc_score(y_test, persist_score)
    ap_persist  = average_precision_score(y_test, persist_score)
    baselines.append({"baseline": "persistence_self_3d", "auc": auc_persist,
                      "ap": ap_persist, "note": "1 if same cell burned in last 3 days"})

    m_fwi = _xgb(best_params)
    m_fwi.fit(X_train[["fwi"]], y_train, eval_set=[(X_val[["fwi"]], y_val)], verbose=False)
    p_fwi = m_fwi.predict_proba(X_test[["fwi"]])[:, 1]
    auc_fwi = roc_auc_score(y_test, p_fwi)
    ap_fwi  = average_precision_score(y_test, p_fwi)
    baselines.append({"baseline": "fwi_only_xgb", "auc": auc_fwi, "ap": ap_fwi,
                      "note": "XGBoost trained on the single FWI feature"})

    baselines.append({"baseline": "FULL_MODEL_v4", "auc": auc_full, "ap": ap_full,
                      "note": "canonical v4 (all features)"})

    for b in baselines:
        print(f"   {b['baseline']:<26} AUC {b['auc']:.4f}  AP {b['ap']:.4f}   {b['note']}")
    pd.DataFrame(baselines).to_csv(OUT_DIR / "baselines_v4.csv", index=False)
    report["D1_baselines"] = baselines

    print("\n" + "=" * 70)
    print("   D2. Block ablation (retrain without each block)")
    print("=" * 70)
    ablation = [{"block_removed": "none (full)", "n_features": len(feature_cols),
                 "auc": auc_full, "ap": ap_full,
                 "delta_auc": 0.0, "delta_ap": 0.0}]
    for block, cols in FEATURE_BLOCKS.items():
        keep = [c for c in feature_cols if c not in cols]
        m = _xgb(best_params)
        m.fit(X_train[keep], y_train, eval_set=[(X_val[keep], y_val)], verbose=False)
        p = m.predict_proba(X_test[keep])[:, 1]
        a, ap = roc_auc_score(y_test, p), average_precision_score(y_test, p)
        ablation.append({"block_removed": block, "n_features": len(keep),
                         "auc": a, "ap": ap,
                         "delta_auc": a - auc_full, "delta_ap": ap - ap_full})
        print(f"   −{block:<14} ({len(keep)} feats)  AUC {a:.4f} (Δ{a-auc_full:+.4f})"
              f"   AP {ap:.4f} (Δ{ap-ap_full:+.4f})")
    pd.DataFrame(ablation).to_csv(OUT_DIR / "block_ablation_v4.csv", index=False)
    report["D2_block_ablation"] = ablation

    shap_summary = None
    try:
        import shap
        print("\n   Computing SHAP values (sampled 20k test rows)...")
        n_s = min(20000, len(X_test))
        Xs = X_test.sample(n=n_s, random_state=RS)
        explainer = shap.TreeExplainer(model)
        sv = explainer.shap_values(Xs)
        mean_abs = np.abs(sv).mean(axis=0)
        gain = model.feature_importances_
        shap_df = pd.DataFrame({
            "feature": feature_cols,
            "gain_importance": gain,
            "shap_mean_abs": mean_abs,
        }).sort_values("shap_mean_abs", ascending=False)
        shap_df.to_csv(OUT_DIR / "shap_importance_v4.csv", index=False)
        shap_summary = shap_df.head(10).to_dict("records")
        print("   Top 5 by SHAP:")
        for r in shap_df.head(5).itertuples():
            print(f"     {r.feature:<20} shap {r.shap_mean_abs:.4f}   gain {r.gain_importance:.4f}")
    except Exception as e:
        print(f"   SHAP skipped ({type(e).__name__}: {e})")
    report["D2_shap_top10"] = shap_summary

    print("\n" + "=" * 70)
    print("   D3. Probability calibration")
    print("=" * 70)
    frac_pos, mean_pred = calibration_curve(y_test, y_prob, n_bins=10, strategy="quantile")
    bins = pd.qcut(y_prob, q=10, duplicates="drop")
    cal_rows, ece = [], 0.0
    for b in bins.categories:
        m = bins == b
        if m.sum() == 0:
            continue
        conf = y_prob[m].mean()
        acc  = y_test[m].mean()
        w    = m.sum() / len(y_test)
        ece += w * abs(acc - conf)
        cal_rows.append({"bin": str(b), "n": int(m.sum()),
                         "mean_pred_prob": float(conf), "frac_positives": float(acc)})
    print(f"   Expected Calibration Error (ECE): {ece:.4f}")
    print(f"   (0 = perfectly calibrated; >0.05 typically worth recalibrating)")
    pd.DataFrame(cal_rows).to_csv(OUT_DIR / "calibration_v4.csv", index=False)
    report["D3_calibration"] = {"ece": float(ece), "bins": cal_rows}

    print("\n" + "=" * 70)
    print("   D4. Spatial error analysis (per subregion)")
    print("=" * 70)
    dft = df_test.copy()
    dft["_prob"] = y_prob
    dft["_y"] = y_test
    spatial_rows = []
    for sub, g in dft.groupby("subregion_id"):
        n_pos = int(g["_y"].sum())
        rec = {"subregion_id": int(sub), "n": int(len(g)), "n_fires": n_pos,
               "fire_rate": float(g["_y"].mean())}
        if n_pos > 0 and n_pos < len(g):
            rec["auc"] = float(roc_auc_score(g["_y"], g["_prob"]))
            rec["ap"]  = float(average_precision_score(g["_y"], g["_prob"]))
        else:
            rec["auc"] = None
            rec["ap"] = None
        spatial_rows.append(rec)
    spatial_df = pd.DataFrame(spatial_rows).sort_values("subregion_id")
    spatial_df.to_csv(OUT_DIR / "spatial_error_v4.csv", index=False)
    for r in spatial_df.itertuples():
        auc_s = f"{r.auc:.4f}" if r.auc is not None else "  n/a"
        ap_s  = f"{r.ap:.4f}" if r.ap is not None else "  n/a"
        print(f"   subregion {r.subregion_id:>2}  n={r.n:>7,}  fires={r.n_fires:>5,}"
              f"  rate={r.fire_rate*100:4.1f}%  AUC {auc_s}  AP {ap_s}")
    report["D4_spatial"] = spatial_rows

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("AlertaFuego V4 — Rigorous Evaluation (Phase A)",
                 fontsize=15, fontweight="bold")

    axes[0, 0].plot([0, 1], [0, 1], "k--", alpha=0.4, label="perfect")
    axes[0, 0].plot(mean_pred, frac_pos, "o-", color="#E8612A",
                    label=f"v4 (ECE={ece:.3f})")
    axes[0, 0].set_title("Reliability curve (calibration)")
    axes[0, 0].set_xlabel("Mean predicted probability")
    axes[0, 0].set_ylabel("Observed fire fraction")
    axes[0, 0].legend(); axes[0, 0].grid(alpha=0.3)

    bn = [b["baseline"] for b in baselines]
    ba = [b["ap"] for b in baselines]
    axes[0, 1].barh(bn, ba, color="#00897B", alpha=0.85)
    axes[0, 1].set_title("Average Precision vs baselines")
    axes[0, 1].set_xlabel("AP"); axes[0, 1].grid(alpha=0.3, axis="x")

    ab = pd.DataFrame(ablation)
    ab2 = ab[ab["block_removed"] != "none (full)"].sort_values("delta_ap")
    axes[1, 0].barh(ab2["block_removed"], ab2["delta_ap"], color="#1565C0", alpha=0.85)
    axes[1, 0].set_title("Δ AP when removing each block (more negative = more important)")
    axes[1, 0].set_xlabel("Δ AP vs full"); axes[1, 0].grid(alpha=0.3, axis="x")

    ss = spatial_df.dropna(subset=["auc"])
    axes[1, 1].bar(ss["subregion_id"].astype(str), ss["auc"], color="#E8612A", alpha=0.85)
    axes[1, 1].axhline(auc_full, color="k", ls="--", alpha=0.5, label=f"global {auc_full:.3f}")
    axes[1, 1].set_title("AUC per subregion")
    axes[1, 1].set_xlabel("subregion_id"); axes[1, 1].set_ylabel("AUC")
    axes[1, 1].legend(); axes[1, 1].grid(alpha=0.3, axis="y")

    plt.tight_layout()
    fig.savefig(OUT_DIR / "eval_plots_v4.png", dpi=150)
    plt.close(fig)

    report["runtime_min"] = (time.time() - t_start) / 60
    with open(OUT_DIR / "evaluation_report_v4.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n" + "=" * 70)
    print(f"   Done in {report['runtime_min']:.1f} min. Outputs in {OUT_DIR}")
    print("=" * 70)

if __name__ == "__main__":
    main()
