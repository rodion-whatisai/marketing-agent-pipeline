# AttentionNeeded — execution-триаж по ad set'ам

Источник: анонимизированный FB lead-gen дашборд (7 городов). Всего ad set: **18**, требуют внимания: **7** (severity != ok).

Пороги (факт CPL / бенчмарк города): `high ≥1.30 · medium ≥1.15 · watch ≥1.00 · ok <1.00`.

| # | severity | city | ad set | CPL факт | бенч | ratio | тренд 5д | «почему» | (ref) человек |
|---|---|---|---|--:|--:|--:|---|---|---|
| 1 | **high** | Dallas | `LG_DAL_Wide_EN` | $14.57 | $10.70 | 1.36 | flat | CPL $14.57 vs бенч $10.70 (+36%); тренд 5д ровный | off |
| 2 | **high** | Ottawa | `LG_OTT_Wide_EN` | $39.50 | $30.07 | 1.31 | rising | CPL $39.50 vs бенч $30.07 (+31%); тренд 5д растёт — ухудшается | off |
| 3 | **medium** | Miami | `LG_MIA_Wide_EN` | $24.36 | $21.13 | 1.15 | falling | CPL $24.36 vs бенч $21.13 (+15%); тренд 5д падает — выправляется | off |
| 4 | **watch** | Vancouver | `LG_VAN_Wide_EN` | $25.68 | $22.57 | 1.14 | flat | CPL $25.68 vs бенч $22.57 (+14%); тренд 5д ровный | off |
| 5 | **watch** | Toronto | `LG_TOR_Wide_EN` | $20.46 | $18.41 | 1.11 | flat | CPL $20.46 vs бенч $18.41 (+11%); тренд 5д ровный | off |
| 6 | **watch** | Houston | `LG_HOU_AUGBDay_ENday` | $10.11 | $9.63 | 1.05 | rising | CPL $10.11 vs бенч $9.63 (+5%); тренд 5д растёт — ухудшается | off |
| 7 | **watch** | Houston | `LG_HOU_Wide_EN` | $10.03 | $9.63 | 1.04 | falling | CPL $10.03 vs бенч $9.63 (+4%); тренд 5д падает — выправляется | limited |
| 8 | **ok** | Ottawa | `LG_OTT_AUGBDay_ENday` | $29.91 | $30.07 | 0.99 | rising | CPL $29.91 ниже бенчмарка $30.07 (-1%) — в норме | off |
| 9 | **ok** | Montreal | `LG_MTL_Wide_EN` | $23.56 | $23.93 | 0.98 | falling | CPL $23.56 ниже бенчмарка $23.93 (-2%) — в норме | off |
| 10 | **ok** | Vancouver | `LG_FLORIDA_Wide_Advantage_EN` | $21.77 | $22.57 | 0.96 | rising | CPL $21.77 ниже бенчмарка $22.57 (-4%) — в норме | limited |
| 11 | **ok** | Montreal | `LG_MTL_AUGBDay_ENday` | $21.92 | $23.93 | 0.92 | rising | CPL $21.92 ниже бенчмарка $23.93 (-8%) — в норме | off |
| 12 | **ok** | Vancouver | `LG_VAN_Wide_EN_bid` | $19.24 | $22.57 | 0.85 | rising | CPL $19.24 ниже бенчмарка $22.57 (-15%) — в норме | off |
| 13 | **ok** | Vancouver | `LG_VAN_AUGBDay_EN` | $19.24 | $22.57 | 0.85 | rising | CPL $19.24 ниже бенчмарка $22.57 (-15%) — в норме | off |
| 14 | **ok** | Montreal | `LG_MTL_Wide_FR` | $20.02 | $23.93 | 0.84 | n/a | CPL $20.02 ниже бенчмарка $23.93 (-16%) — в норме | off |
| 15 | **ok** | Miami | `LG_MIA_AUGBDay_ENday` | $17.00 | $21.13 | 0.81 | rising | CPL $17.00 ниже бенчмарка $21.13 (-20%) — в норме | off |
| 16 | **ok** | Toronto | `LG_TOR_AUGBDay_EN` | $13.21 | $18.41 | 0.72 | falling | CPL $13.21 ниже бенчмарка $18.41 (-28%) — в норме | off |
| 17 | **ok** | Dallas | `LG_DAL_AUGBDay_EN` | $7.66 | $10.70 | 0.72 | rising | CPL $7.66 ниже бенчмарка $10.70 (-28%) — в норме | off |
| 18 | **ok** | Montreal | `LG_MTL_AUGBDay_FRday` | $16.63 | $23.93 | 0.69 | rising | CPL $16.63 ниже бенчмарка $23.93 (-31%) — в норме | off |

> `(ref) человек` — что в исходном дашборде сделал оператор. Это **контекст**, не рекомендация: в августовском срезе оператор сворачивал почти всё (конец месяца), поэтому `off` стоит даже у дешёвых ad set'ов. AttentionNeeded ранжирует по факту vs бенчмарк — воспроизводимо и без этого шума.
