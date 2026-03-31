# Databricks notebook source
# MAGIC %md
# MAGIC # Grid Visualization — Mapa de subregiones
# MAGIC
# MAGIC Genera un mapa de los 2,266 nodos de `aux_grid_pampa` coloreados
# MAGIC por subregión RESOLVE 2017 y lo guarda en el Volume como referencia.
# MAGIC
# MAGIC **Ejecutar después de subregion_classification.**

# COMMAND ----------

# MAGIC %pip install geopandas pyogrio matplotlib --quiet

# COMMAND ----------

import geopandas as gpd
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import urllib.request, zipfile, os
import warnings
warnings.filterwarnings("ignore")

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

TABLE        = "fire_risk_project.00_landing.aux_grid_pampa"
OUTPUT_IMAGE = "/Volumes/fire_risk_project/00_landing/grid_setup/grilla_pampa.png"
TMP_DIR      = "/tmp/fire_grid"
EXTENT       = (-69.5, -54.5, -43.5, -26.5)

COLORES_SUBREGION = {
    "Pampa Humeda":    "#2ecc71",
    "Delta/Litoral":   "#3498db",
    "Monte":           "#e67e22",
    "Espinal":         "#16a085",
    "Chaco Seco":      "#e74c3c",
    "Chaco Humedo":    "#c0392b",
    "Patagonia norte": "#95a5a6",
    "Otro":            "#dfe6e9",
}
ORDEN_LEYENDA = [
    "Pampa Humeda", "Espinal", "Monte", "Chaco Seco",
    "Chaco Humedo", "Delta/Litoral", "Patagonia norte", "Otro"
]

NE_URL = "https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_admin_0_countries.zip"

# COMMAND ----------

# MAGIC %md ## 1 · Cargar datos

# COMMAND ----------

df_grid = spark.table(TABLE).filter("is_valid = true").toPandas()
print(f"Nodos cargados: {len(df_grid):,}")
print(f"\nDistribución por subregión:")
print(df_grid["subregion_name"].value_counts().to_string())

# COMMAND ----------

# MAGIC %md ## 2 · Contornos Natural Earth

# COMMAND ----------

os.makedirs(TMP_DIR, exist_ok=True)
NE_ZIP = f"{TMP_DIR}/ne_10m_countries.zip"
NE_SHP = f"{TMP_DIR}/ne_10m_admin_0_countries.shp"

if not os.path.exists(NE_SHP):
    print("Descargando contornos Natural Earth...")
    urllib.request.urlretrieve(NE_URL, NE_ZIP)
    with zipfile.ZipFile(NE_ZIP) as z:
        z.extractall(TMP_DIR)

world     = gpd.read_file(NE_SHP, engine="pyogrio").to_crs("EPSG:4326")
argentina = world[world["NAME"] == "Argentina"]
vecinos   = world[world["NAME"].isin(["Chile", "Uruguay", "Brazil", "Paraguay", "Bolivia"])]

# COMMAND ----------

# MAGIC %md ## 3 · Generar y guardar mapa

# COMMAND ----------

fig, ax = plt.subplots(figsize=(10, 14), dpi=130)

vecinos.plot(ax=ax, color="#f0f0f0", edgecolor="#cccccc", linewidth=0.5, zorder=1)
argentina.plot(ax=ax, color="#fafafa", edgecolor="#555555", linewidth=1.0, zorder=2)

for nombre, grupo in df_grid.groupby("subregion_name"):
    color = COLORES_SUBREGION.get(nombre, "#bdc3c7")
    ax.scatter(
        grupo["longitude"], grupo["latitude"],
        c=color, s=22, alpha=0.9, zorder=3, linewidths=0
    )

handles = [
    mpatches.Patch(
        color=COLORES_SUBREGION.get(n, "#bdc3c7"),
        label=f"{n}  ({len(df_grid[df_grid['subregion_name'] == n])} nodos)"
    )
    for n in ORDEN_LEYENDA
    if len(df_grid[df_grid["subregion_name"] == n]) > 0
]
ax.legend(handles=handles, loc="lower left", fontsize=9,
          framealpha=0.92, title="Subregión RESOLVE 2017", title_fontsize=9)

ax.set_xlim(EXTENT[0], EXTENT[1])
ax.set_ylim(EXTENT[2], EXTENT[3])
ax.set_xlabel("Longitud", fontsize=11)
ax.set_ylabel("Latitud", fontsize=11)
ax.set_title(
    f"Grilla Pampeana — aux_grid_pampa\n"
    f"Resolución 0.25°  ·  {len(df_grid):,} nodos  ·  Subregiones RESOLVE 2017",
    fontsize=12, pad=12
)
ax.grid(True, alpha=0.2, linestyle="--", linewidth=0.5)
ax.tick_params(labelsize=9)
plt.tight_layout()

plt.savefig(OUTPUT_IMAGE, dpi=150, bbox_inches="tight")
display(fig)
plt.close(fig)
print(f"Mapa guardado: {OUTPUT_IMAGE}")

dbutils.notebook.exit(f"OK: mapa guardado en {OUTPUT_IMAGE}")
