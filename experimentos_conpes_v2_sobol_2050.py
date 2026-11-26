#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MOTOR SSOT v2.2 — PARIDAD STELLA + RK4 + BARLAS + LTM + SOBOL (PANEL A/B) + BARRIDO 1D
Proyecto: `2026_conpes_ctei_colombia` (*Science and Public Policy*, USD 0 — Metis Lab)

Lee automáticamente los parámetros calibrados de `redisenio_merlin/reporte_calibracion_wb_conpes_2015_2050.md`
(100% SSOT sin valores hardcodeados divergentes), corre:
  1. Los 7 escenarios al 2050 (2015–2050, RK4, DT=0.25) y exporta sus CSVs a `modelo_stella/corridas_csv/`.
  2. Confrontación directa contra el CSV exportado de Stella Architect (`--comparar-stella`).
  3. Convergencia numérica RK4 a 3 pasos (`DT=0.25 -> 0.125 -> 0.0625`) + contraste Euler + Barlas unclamped.
  4. Dominancia LTM por fases (2015–2026 vs. 2027–2050).
  5. Sobol Panel A (7 parámetros clave incluyendo `delay_time_rd_transfer` y `gdp_rd_allocation_target`, ±25%, IC 95%).
  6. Sobol Panel B (3 palancas PND en [0,1]^3, N=1,280, IC 95%) + Invarianza ordinal.
  7. Barrido 1D contrafactual de Presupuesto Aislado (`Lever 1` en [0, 1] con Ecosistema y Retención en Baseline),
     blindando el manuscrito contra observaciones epistémicas (Motor #24).

Uso:
  python 03_proyectos/2026_conpes_ctei_colombia/modelo_stella/experimentos_conpes_v2_sobol_2050.py
  python 03_proyectos/2026_conpes_ctei_colombia/modelo_stella/experimentos_conpes_v2_sobol_2050.py --comparar-stella ~/Downloads/stella_base_v22.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import re
import sqlite3
import statistics
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from SALib.analyze import sobol as sobol_analyze
    try:
        from SALib.sample import sobol as sobol_sample
    except ImportError:
        from SALib.sample import saltelli as sobol_sample
    TIENE_SALIB = True
except ImportError:
    TIENE_SALIB = False


STOCK_NAMES: List[str] = [
    "talent_in_training",
    "active_sti_talent",
    "diaspora_talent",
    "traditional_firms",
    "adopting_firms",
    "innovative_firms",
    "rd_projects_pipeline",
    "transferred_solutions",
    "active_industrial_solutions",
    "rd_budget_stock",
    "gdp",
]

INIT_V22: Dict[str, float] = {
    "talent_in_training": 2400.0,
    "active_sti_talent": 3550.0,
    "diaspora_talent": 1800.0,
    "traditional_firms": 84000.0,
    "adopting_firms": 8000.0,
    "innovative_firms": 8000.0,
    "rd_projects_pipeline": 950.0,
    "transferred_solutions": 350.0,
    "active_industrial_solutions": 600.0,
    "rd_budget_stock": 450.0,
    "gdp": 293480.0,
}

PARAMS_DEFAULT_V22: Dict[str, float] = {
    "baseline_gdp_growth_rate": 0.028,
    "innovation_gdp_elasticity": 0.032,
    "gdp_rd_allocation_target": 0.0029,
    "external_rd_funding_base": 80.0,
    "budget_min_liquidity_time": 0.5,
    "budget_execution_efficiency": 0.75,
    "base_talent_intake": 1150.0,
    "cost_per_talent_trained": 0.28,
    "avg_training_duration": 4.0,
    "talent_per_innovative_firm": 1.5,
    "talent_per_adopting_firm": 0.20,
    "base_emigration_rate": 0.055,
    "excess_emigration_rate": 0.15,
    "base_repatriation_rate": 0.03,
    "retirement_rate": 0.03,
    "firm_creation_rate_base": 0.03,
    "firm_exit_rate_base": 0.025,
    "innovative_exit_multiplier": 0.50,
    "base_adoption_rate": 0.022,
    "avg_adoption_duration": 4.0,
    "base_regression_rate": 0.05,
    "project_proposal_propensity": 0.145,
    "avg_cost_per_project": 2.4,
    "avg_project_duration": 3.0,
    "ref_solutions_half_sat": 600.0,
    "incose_rigor_index": 0.60,
    "gov_uni_ind_linkage": 0.50,
    "delay_time_rd_transfer": 4.0,
    "stagnation_rate_base": 0.14,
    "solution_lifetime": 8.0,
    "attractiveness_floor": 0.50,
    "attractiveness_saturation_weight": 0.50,
    "attractiveness_innovation_weight": 1.0,
    "incose_weight": 0.70,
    "linkage_weight": 0.30,
    "pnd_start_year": 2026.75,
}

ESCENARIOS_PND: Dict[str, Dict[str, float]] = {
    "01_Status_Quo_CONPES": {},
    "02_Transicion_Rezagada": {
        "budget_execution_efficiency": 0.55,
        "gov_uni_ind_linkage": 0.35,
        "excess_emigration_rate": 0.22,
    },
    "03_PND_Solo_Presupuesto": {
        "gdp_rd_allocation_target": 0.0060,
        "budget_execution_efficiency": 0.90,
    },
    "04_PND_Ecosistema_Retencion": {
        "incose_rigor_index": 0.85,
        "gov_uni_ind_linkage": 0.80,
        "delay_time_rd_transfer": 2.5,
        "excess_emigration_rate": 0.06,
    },
    "05_PND_Integral_2026_2030": {
        "gdp_rd_allocation_target": 0.0060,
        "budget_execution_efficiency": 0.90,
        "incose_rigor_index": 0.85,
        "gov_uni_ind_linkage": 0.80,
        "delay_time_rd_transfer": 2.5,
        "excess_emigration_rate": 0.06,
        "base_repatriation_rate": 0.07,
    },
    "06_Barlas_Extremo1_Colapso": {
        "gdp_rd_allocation_target": 0.0005,
        "budget_execution_efficiency": 0.25,
        "excess_emigration_rate": 0.35,
        "delay_time_rd_transfer": 8.0,
    },
    "07_Barlas_Extremo2_Expansion": {
        "gdp_rd_allocation_target": 0.0100,
        "budget_execution_efficiency": 0.98,
        "incose_rigor_index": 0.95,
        "gov_uni_ind_linkage": 0.95,
        "delay_time_rd_transfer": 1.5,
    },
}


def cargar_params_calibrados_desde_ssot(proy_dir: Path) -> Tuple[Dict[str, float], str]:
    """Lee automáticamente los parámetros calibrados de v2.2 desde reporte_calibracion o prompt_merlin."""
    params = dict(PARAMS_DEFAULT_V22)
    red_dir = proy_dir / "redisenio_merlin"
    fuente = "Valores por defecto v2.2"
    for fname in ("reporte_calibracion_wb_conpes_2015_2050.md", "prompt_merlin_calibracion_conpes.txt"):
        p = red_dir / fname
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        leidos = 0
        for k in list(params.keys()):
            m = re.search(rf"`{re.escape(k)}\s*=\s*([0-9]+(?:\.[0-9]+)?)`", txt)
            if m:
                params[k] = float(m.group(1))
                leidos += 1
        if leidos >= 5:
            fuente = f"`{fname}` ({leidos} parámetros sincronizados)"
            break
    return params, fuente


def derivadas_v22(
    t: float,
    y: np.ndarray,
    p_base: Dict[str, float],
    overrides_pnd: Dict[str, float],
    aplicar_clamp: bool = True,
) -> Tuple[np.ndarray, Dict[str, float]]:
    p = dict(p_base)
    if t >= p["pnd_start_year"] and overrides_pnd:
        p.update(overrides_pnd)

    vals = [max(0.0, float(v)) for v in y] if aplicar_clamp else [float(v) for v in y]
    (
        t_train, t_act, t_diasp,
        f_trad, f_adop, f_innov,
        p_pipe, p_trans, p_sol,
        b_stock, gdp_stock,
    ) = vals

    total_people = t_train + t_act + t_diasp
    total_firms = f_trad + f_adop + f_innov
    total_projects = p_pipe + p_trans + p_sol
    innov_share = f_innov / max(1.0, total_firms)

    gdp_growth_rate = p["baseline_gdp_growth_rate"] + p["innovation_gdp_elasticity"] * innov_share
    d_gdp = gdp_stock * gdp_growth_rate

    sat_sol = (p_sol / p["ref_solutions_half_sat"]) / (1.0 + p_sol / p["ref_solutions_half_sat"])
    sti_attract = (
        p["attractiveness_floor"]
        + p["attractiveness_saturation_weight"] * sat_sol
        + p["attractiveness_innovation_weight"] * innov_share
    )

    des_proj_starts = t_act * p["project_proposal_propensity"]
    des_tal_intake = p["base_talent_intake"] * sti_attract
    rd_proj_dem = des_proj_starts * p["avg_cost_per_project"]
    tal_prog_dem = des_tal_intake * p["cost_per_talent_trained"]
    tot_fund_dem = rd_proj_dem + tal_prog_dem
    max_afford = b_stock / p["budget_min_liquidity_time"]
    exec_ratio = min(1.0, (max_afford * p["budget_execution_efficiency"]) / max(0.001, tot_fund_dem))

    gdp_contrib = gdp_stock * p["gdp_rd_allocation_target"]
    ext_fund = p["external_rd_funding_base"]
    rd_proj_fund = rd_proj_dem * exec_ratio
    tal_prog_fund = tal_prog_dem * exec_ratio

    new_tal_form = tal_prog_fund / p["cost_per_talent_trained"]
    tal_grad = t_train / p["avg_training_duration"]
    dom_tal_dem = f_innov * p["talent_per_innovative_firm"] + f_adop * p["talent_per_adopting_firm"]
    unabs_tal = max(0.0, t_act - dom_tal_dem) if aplicar_clamp else (t_act - dom_tal_dem)
    tal_emig = t_act * p["base_emigration_rate"] + unabs_tal * p["excess_emigration_rate"]
    tal_repat = t_diasp * p["base_repatriation_rate"] * sti_attract
    tal_retir = t_act * p["retirement_rate"]

    new_firm = total_firms * p["firm_creation_rate_base"]
    firm_adopt = f_trad * p["base_adoption_rate"] * (p_sol / (p_sol + p["ref_solutions_half_sat"])) * p["gov_uni_ind_linkage"]
    firm_conv = f_adop * p["incose_rigor_index"] / p["avg_adoption_duration"]
    firm_reg = f_innov * p["base_regression_rate"] * (1.0 - p["incose_rigor_index"])
    f_trad_exit = f_trad * p["firm_exit_rate_base"]
    f_adop_exit = f_adop * p["firm_exit_rate_base"]
    f_innov_exit = f_innov * (p["firm_exit_rate_base"] * p["innovative_exit_multiplier"])

    new_rd_init = rd_proj_fund / p["avg_cost_per_project"]
    proj_comp = p_pipe / p["avg_project_duration"]
    trans_eff = p["incose_weight"] * p["incose_rigor_index"] + p["linkage_weight"] * p["gov_uni_ind_linkage"]
    sol_trans = p_trans * trans_eff / p["delay_time_rd_transfer"]
    trans_stag = p_trans * p["stagnation_rate_base"]
    sol_obsol = p_sol / p["solution_lifetime"]

    dy = np.array([
        new_tal_form - tal_grad,
        tal_grad + tal_repat - tal_emig - tal_retir,
        tal_emig - tal_repat,
        new_firm - firm_adopt - f_trad_exit,
        firm_adopt + firm_reg - firm_conv - f_adop_exit,
        firm_conv - firm_reg - f_innov_exit,
        new_rd_init - proj_comp,
        proj_comp - sol_trans - trans_stag,
        sol_trans - sol_obsol,
        gdp_contrib + ext_fund - rd_proj_fund - tal_prog_fund,
        d_gdp,
    ], dtype=float)

    # Proxy LTM de 4 macro-bucles (normalizado a 100%)
    w_r1_fiscal = (rd_proj_fund / max(1.0, b_stock)) + (sol_trans / max(1.0, p_sol))
    w_r2_talento = (tal_grad + tal_repat) / max(1.0, t_act)
    w_b1_fuga = tal_emig / max(1.0, t_act)
    w_b2_valle = (trans_stag / max(1.0, p_trans)) + max(0.0, 1.0 - exec_ratio)
    tot_w = max(1e-9, w_r1_fiscal + w_r2_talento + w_b1_fuga + w_b2_valle)

    res_people = abs((dy[0] + dy[1] + dy[2]) - (new_tal_form - tal_retir))
    res_firms = abs((dy[3] + dy[4] + dy[5]) - (new_firm - f_trad_exit - f_adop_exit - f_innov_exit))
    res_proj = abs((dy[6] + dy[7] + dy[8]) - (new_rd_init - trans_stag - sol_obsol))

    return dy, {
        "total_sti_people": total_people,
        "total_firms": total_firms,
        "total_projects": total_projects,
        "innovative_firm_share": innov_share,
        "budget_execution_ratio": exec_ratio,
        "project_completion_rate": proj_comp,
        "rd_spending_gdp_share": (rd_proj_fund + tal_prog_fund) / max(1.0, gdp_stock),
        "ltm_r1_fiscal_pct": 100.0 * w_r1_fiscal / tot_w,
        "ltm_r2_talent_pct": 100.0 * w_r2_talento / tot_w,
        "ltm_b1_braindrain_pct": 100.0 * w_b1_fuga / tot_w,
        "ltm_b2_valley_ration_pct": 100.0 * w_b2_valle / tot_w,
        "max_cons_res": max(res_people, res_firms, res_proj),
    }


def simular(
    params: Dict[str, float],
    overrides_pnd: Optional[Dict[str, float]] = None,
    t0: float = 2015.0,
    tf: float = 2050.0,
    dt: float = 0.25,
    metodo: str = "RK4",
    aplicar_clamp: bool = True,
) -> Tuple[pd.DataFrame, float]:
    ov = overrides_pnd or {}
    y = np.array([INIT_V22[k] for k in STOCK_NAMES], dtype=float)
    n_steps = int(round((tf - t0) / dt))
    filas: List[Dict[str, float]] = []
    max_res = 0.0

    for s in range(n_steps + 1):
        t = round(t0 + s * dt, 6)
        k1, meta = derivadas_v22(t, y, params, ov, aplicar_clamp=aplicar_clamp)
        max_res = max(max_res, meta["max_cons_res"])

        if abs(t - round(t)) < 1e-6 or s == n_steps:
            fila = {"Time": round(t, 2)}
            for nm, val in zip(STOCK_NAMES, y):
                fila[nm] = round(float(val), 4)
            for k, v in meta.items():
                if k != "max_cons_res":
                    fila[k] = round(float(v), 4)
            filas.append(fila)

        if s == n_steps:
            break

        if metodo.lower() == "rk4":
            y2 = np.maximum(0.0, y + 0.5 * dt * k1) if aplicar_clamp else (y + 0.5 * dt * k1)
            k2, _ = derivadas_v22(t + 0.5 * dt, y2, params, ov, aplicar_clamp=aplicar_clamp)
            y3 = np.maximum(0.0, y + 0.5 * dt * k2) if aplicar_clamp else (y + 0.5 * dt * k2)
            k3, _ = derivadas_v22(t + 0.5 * dt, y3, params, ov, aplicar_clamp=aplicar_clamp)
            y4 = np.maximum(0.0, y + dt * k3) if aplicar_clamp else (y + dt * k3)
            k4, _ = derivadas_v22(t + dt, y4, params, ov, aplicar_clamp=aplicar_clamp)
            y = y + (dt / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)
        else:
            y = y + dt * k1

        if aplicar_clamp:
            y = np.maximum(0.0, y)

    return pd.DataFrame(filas), max_res


def ejecutar_sobol_panel_a(params: Dict[str, float], n_base: int = 256) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    if not TIENE_SALIB:
        return pd.DataFrame(), {"ejecutado": False}

    nombres = [
        "gdp_rd_allocation_target",
        "budget_execution_efficiency",
        "incose_rigor_index",
        "gov_uni_ind_linkage",
        "delay_time_rd_transfer",
        "stagnation_rate_base",
        "excess_emigration_rate",
    ]
    bounds = [
        [params[k] * 0.75, min(0.98, params[k] * 1.25) if k in ("budget_execution_efficiency", "incose_rigor_index", "gov_uni_ind_linkage") else params[k] * 1.25]
        for k in nombres
    ]
    problem = {"num_vars": len(nombres), "names": nombres, "bounds": bounds}
    X = sobol_sample.sample(problem, n_base, calc_second_order=False)
    n_tot = len(X)

    y_sol, y_firms, y_tal, y_gdp = np.zeros(n_tot), np.zeros(n_tot), np.zeros(n_tot), np.zeros(n_tot)
    for i, row in enumerate(X):
        p_i = dict(params)
        for j, nm in enumerate(nombres):
            p_i[nm] = float(row[j])
        df_i, _ = simular(p_i, {}, dt=0.5, tf=2050.0, metodo="RK4")
        u = df_i.iloc[-1]
        y_sol[i] = float(u["active_industrial_solutions"])
        y_firms[i] = float(u["innovative_firms"])
        y_tal[i] = float(u["active_sti_talent"])
        y_gdp[i] = float(u["gdp"])

    sb_s = sobol_analyze.analyze(problem, y_sol, calc_second_order=False, conf_level=0.95, print_to_console=False)
    sb_f = sobol_analyze.analyze(problem, y_firms, calc_second_order=False, conf_level=0.95, print_to_console=False)
    sb_t = sobol_analyze.analyze(problem, y_tal, calc_second_order=False, conf_level=0.95, print_to_console=False)

    # Invarianza ordinal: 05_PND_Integral > 01_Status_Quo en Soluciones y Empresas Innovadoras
    rng = np.random.default_rng(2026)
    idxs = rng.choice(n_tot, size=min(60, n_tot), replace=False)
    inv_ok = 0
    for idx in idxs:
        p0 = dict(params)
        for j, nm in enumerate(nombres):
            p0[nm] = float(X[idx][j])
        u0 = simular(p0, {}, dt=0.5, tf=2050.0)[0].iloc[-1]
        u5 = simular(p0, ESCENARIOS_PND["05_PND_Integral_2026_2030"], dt=0.5, tf=2050.0)[0].iloc[-1]
        if (u5["active_industrial_solutions"] > u0["active_industrial_solutions"]) and (u5["innovative_firms"] > u0["innovative_firms"]):
            inv_ok += 1

    df_a = pd.DataFrame({
        "Parametro": nombres,
        "S1_Soluc2050": np.round(sb_s["S1"], 3),
        "ST_Soluc2050_CI95": [f"{v:.3f} ± {c:.3f}" for v, c in zip(sb_s["ST"], sb_s["ST_conf"])],
        "S1_EmpInnov2050": np.round(sb_f["S1"], 3),
        "ST_EmpInnov2050_CI95": [f"{v:.3f} ± {c:.3f}" for v, c in zip(sb_f["ST"], sb_f["ST_conf"])],
        "ST_Talento2050_CI95": [f"{v:.3f} ± {c:.3f}" for v, c in zip(sb_t["ST"], sb_t["ST_conf"])],
        "_sort": sb_s["ST"],
    }).sort_values("_sort", ascending=False).drop(columns=["_sort"])

    meta = {
        "ejecutado": True,
        "n_base": n_base,
        "n_corridas": int(n_tot),
        "soluciones_2050_p10_p90": [round(float(np.percentile(y_sol, 10)), 1), round(float(np.percentile(y_sol, 90)), 1)],
        "emp_innov_2050_p10_p90": [round(float(np.percentile(y_firms, 10)), 1), round(float(np.percentile(y_firms, 90)), 1)],
        "invarianza_ordinal_05_gt_01_pct": round(100.0 * inv_ok / len(idxs), 1),
    }
    return df_a, meta


def ejecutar_sobol_panel_b(params: Dict[str, float], n_pol: int = 256) -> Tuple[pd.DataFrame, int]:
    if not TIENE_SALIB:
        return pd.DataFrame(), 0

    nombres = ["Lever1_Fiscal_Execution", "Lever2_INCOSE_Linkage_Delay", "Lever3_Talent_Retention"]
    problem = {"num_vars": 3, "names": nombres, "bounds": [[0.0, 1.0], [0.0, 1.0], [0.0, 1.0]]}
    X = sobol_sample.sample(problem, n_pol, calc_second_order=False)
    n_tot = len(X)

    y_sol, y_firms, y_tal, y_bud = np.zeros(n_tot), np.zeros(n_tot), np.zeros(n_tot), np.zeros(n_tot)
    for i, row in enumerate(X):
        l1, l2, l3 = float(row[0]), float(row[1]), float(row[2])
        ov = {
            "gdp_rd_allocation_target": 0.0029 + 0.0041 * l1,
            "budget_execution_efficiency": 0.65 + 0.28 * l1,
            "incose_rigor_index": 0.50 + 0.40 * l2,
            "gov_uni_ind_linkage": 0.40 + 0.45 * l2,
            "delay_time_rd_transfer": 4.5 - 2.2 * l2,
            "excess_emigration_rate": 0.20 - 0.15 * l3,
            "base_repatriation_rate": 0.02 + 0.06 * l3,
        }
        u = simular(params, ov, dt=0.5, tf=2050.0)[0].iloc[-1]
        y_sol[i] = float(u["active_industrial_solutions"])
        y_firms[i] = float(u["innovative_firms"])
        y_tal[i] = float(u["active_sti_talent"])
        y_bud[i] = float(u["rd_budget_stock"])

    sb_s = sobol_analyze.analyze(problem, y_sol, calc_second_order=False, conf_level=0.95, print_to_console=False)
    sb_f = sobol_analyze.analyze(problem, y_firms, calc_second_order=False, conf_level=0.95, print_to_console=False)
    sb_t = sobol_analyze.analyze(problem, y_tal, calc_second_order=False, conf_level=0.95, print_to_console=False)
    sb_b = sobol_analyze.analyze(problem, y_bud, calc_second_order=False, conf_level=0.95, print_to_console=False)

    df_b = pd.DataFrame({
        "Palanca_PND_2026_2030": nombres,
        "S1_Soluc": np.round(sb_s["S1"], 3),
        "ST_Soluc_CI95": [f"{v:.3f} ± {c:.3f}" for v, c in zip(sb_s["ST"], sb_s["ST_conf"])],
        "S1_EmpInnov": np.round(sb_f["S1"], 3),
        "ST_EmpInnov_CI95": [f"{v:.3f} ± {c:.3f}" for v, c in zip(sb_f["ST"], sb_f["ST_conf"])],
        "ST_Talento_CI95": [f"{v:.3f} ± {c:.3f}" for v, c in zip(sb_t["ST"], sb_t["ST_conf"])],
        "ST_Presup_CI95": [f"{v:.3f} ± {c:.3f}" for v, c in zip(sb_b["ST"], sb_b["ST_conf"])],
    })
    return df_b, n_tot


def ejecutar_barrido_1d_fiscal(params: Dict[str, float]) -> pd.DataFrame:
    """
    Barrido 1D de Palanca 1 (Presupuesto y Ejecución aislados en [0, 1] manteniendo Ecosistema y Retención en Baseline).
    Demuestra cuantitativamente el techo de Empresas Innovadoras cuando solo se inyecta presupuesto.
    """
    filas: List[Dict[str, Any]] = []
    for l1 in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        rd_t = 0.0029 + 0.0041 * l1
        eff_t = 0.75 + 0.18 * l1
        u = simular(params, {"gdp_rd_allocation_target": rd_t, "budget_execution_efficiency": eff_t}, dt=0.25, tf=2050.0)[0].iloc[-1]
        filas.append({
            "Lever1_Level": round(l1, 1),
            "RD_GDP_Target_pct": round(rd_t * 100.0, 2),
            "Exec_Eff": round(eff_t, 2),
            "Talento_2050": round(float(u["active_sti_talent"]), 1),
            "EmpInnov_2050": round(float(u["innovative_firms"]), 1),
            "SolucInd_2050": round(float(u["active_industrial_solutions"]), 1),
            "Presup_2050_M": round(float(u["rd_budget_stock"]), 1),
            "ExecRat_2050": round(float(u["budget_execution_ratio"]), 2),
            "Supera_20k_EmpInnov": "YES" if float(u["innovative_firms"]) >= 20000.0 else "NO",
        })
    return pd.DataFrame(filas)


def _normalizar_var(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


def _desempaquetar_blob_floats(blob: bytes, numvals: int) -> List[float]:
    """Desempaqueta vectores binarios de Stella (.isdb: float64, float32 o zlib)."""
    if not blob or numvals <= 0:
        return []
    if len(blob) == numvals * 8:
        return list(struct.unpack(f"<{numvals}d", blob))
    if len(blob) == numvals * 4:
        return list(struct.unpack(f"<{numvals}f", blob))
    try:
        dec = zlib.decompress(blob)
        if len(dec) == numvals * 8:
            return list(struct.unpack(f"<{numvals}d", dec))
        if len(dec) == numvals * 4:
            return list(struct.unpack(f"<{numvals}f", dec))
    except Exception:
        pass
    return []


def localizar_isdb_mas_reciente(mod_dir: Path) -> Optional[Path]:
    """Detecta automáticamente el archivo .isdb más reciente guardado por Stella en modelo_stella/."""
    if not mod_dir.exists():
        return None
    candidatos = sorted(
        mod_dir.glob("*.isdb"),
        key=lambda p: (1 if "v22" in p.name.lower() or "calibrado" in p.name.lower() else 0, p.stat().st_mtime),
        reverse=True,
    )
    return candidatos[0] if candidatos else None


def leer_stella_isdb(
    ruta_isdb: Path,
    csv_dir: Optional[Path] = None,
) -> Tuple[Dict[int, Dict[str, float]], str]:
    """
    Lee directamente la base SQLite `.isdb` de Stella Architect (última corrida completa),
    extrae las series temporales binarias sin exportación manual y archiva un CSV limpio.
    """
    conn = sqlite3.connect(str(ruta_isdb))
    try:
        cur = conn.cursor()
        corridas = cur.execute(
            "SELECT id, name, starttime, stoptime, dt FROM runs ORDER BY id DESC"
        ).fetchall()
        if not corridas:
            raise RuntimeError(f"No se encontraron corridas en {ruta_isdb.name}.")

        run_sel = None
        for r in corridas:
            n_rows = cur.execute("SELECT COUNT(*) FROM run_data WHERE run = ?", (r[0],)).fetchone()[0]
            if n_rows > 0:
                run_sel = r
                break
        if run_sel is None:
            raise RuntimeError(f"Las corridas de {ruta_isdb.name} no tienen datos en `run_data`.")

        run_id, run_name, t_start, t_stop, _dt = run_sel
        row_t = cur.execute(
            "SELECT numvals, times FROM run_time WHERE run = ? ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        tiempos_glob: List[float] = []
        if row_t and row_t[1]:
            tiempos_glob = _desempaquetar_blob_floats(row_t[1], int(row_t[0]))

        registros = cur.execute(
            """
            SELECT v.name, rd.numvals, rd.data
            FROM run_data rd
            JOIN variables v ON v.id = rd.variable
            WHERE rd.run = ?
            """,
            (run_id,),
        ).fetchall()

        mejor_por_var: Dict[str, Tuple[int, List[float]]] = {}
        for vname, numvals, blob in registros:
            var = _normalizar_var(str(vname).split(".")[-1])
            vals = _desempaquetar_blob_floats(blob, int(numvals))
            if not vals:
                continue
            if var not in mejor_por_var or len(vals) > mejor_por_var[var][0]:
                mejor_por_var[var] = (len(vals), vals)

        stella: Dict[int, Dict[str, float]] = {}
        for var, (nv, vals) in mejor_por_var.items():
            if nv == 1:
                for yr in range(int(round(t_start)), int(round(t_stop)) + 1):
                    stella.setdefault(yr, {})[var] = float(vals[0])
                continue
            if len(tiempos_glob) == nv:
                t_vec = tiempos_glob
            else:
                paso = (float(t_stop) - float(t_start)) / max(1, nv - 1)
                t_vec = [float(t_start) + i * paso for i in range(nv)]
            for tv, vv in zip(t_vec, vals):
                if abs(tv - round(tv)) < 1e-4:
                    stella.setdefault(int(round(tv)), {})[var] = float(vv)

        if csv_dir and stella:
            out_auto = csv_dir / "stella_auto_desde_isdb.csv"
            anios_ord = sorted(stella.keys())
            cols_ord = [c for c in STOCK_NAMES if any(c in stella[y] for y in anios_ord)]
            with open(out_auto, "w", encoding="utf-8", newline="") as f_out:
                w = csv.writer(f_out)
                w.writerow(["Time"] + cols_ord)
                for y in anios_ord:
                    w.writerow([y] + [round(stella[y].get(c, float("nan")), 6) for c in cols_ord])

        return stella, f"{ruta_isdb.name} ({run_name})"
    finally:
        conn.close()


def comparar_mapa_stella(
    df_base: pd.DataFrame,
    stella: Dict[int, Dict[str, float]],
    etiqueta_fuente: str,
) -> str:
    """Genera tabla de paridad numérica MAE / RMSE / Error Relativo Máx. (%) contra Python v2.2."""
    py_map = {int(round(float(r["Time"]))): r for _, r in df_base.iterrows()}
    comunes = sorted(set(py_map.keys()) & set(stella.keys()))
    lines = [
        f"| Stock (Stella `{etiqueta_fuente}` vs. Python v2.2; {len(comunes)} años) | MAE | RMSE | Error Rel. Máx. (%) | Estado (<1.0%) |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ]
    for st in STOCK_NAMES:
        pares = [(float(py_map[t][st]), float(stella[t][st])) for t in comunes if st in stella[t]]
        if not pares:
            lines.append(f"| `{st}` | — | — | — | No presente en Stella |")
            continue
        mae = statistics.mean(abs(a - b) for a, b in pares)
        rmse = math.sqrt(statistics.mean((a - b) ** 2 for a, b in pares))
        max_rel = max(100.0 * abs(a - b) / max(1e-6, abs(a), abs(b)) for a, b in pares)
        lines.append(f"| `{st}` | {mae:.4f} | {rmse:.4f} | {max_rel:.3f}% | **{'PASS' if max_rel < 1.0 else 'REVISAR'}** |")
    return "\n".join(lines)


def comparar_csv_stella(df_base: pd.DataFrame, ruta_stella: Path) -> str:
    """Confronta CSV o .isdb de Stella Architect contra el testigo Python v2.2."""
    if ruta_stella.suffix.lower() == ".isdb":
        stella_map, etiq = leer_stella_isdb(ruta_stella)
        return comparar_mapa_stella(df_base, stella_map, etiq)

    with open(ruta_stella, "r", encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.reader(f) if r and any(c.strip() for c in r)]
    if len(rows) < 2:
        return "> CSV de Stella vacío o ilegible."

    hdr = [c.strip() for c in rows[0]]
    es_horiz = False
    if len(hdr) >= 3:
        try:
            float(hdr[1])
            float(hdr[2])
            es_horiz = True
        except ValueError:
            es_horiz = False

    stella: Dict[int, Dict[str, float]] = {}
    if es_horiz:
        tiempos = [int(round(float(x))) for x in hdr[1:] if x.strip()]
        for r in rows[1:]:
            var = _normalizar_var(r[0].split(".")[-1])
            for t_v, cell in zip(tiempos, r[1:]):
                if cell.strip():
                    try:
                        stella.setdefault(t_v, {})[var] = float(cell)
                    except ValueError:
                        continue
    else:
        cols = [_normalizar_var(c.split(".")[-1]) for c in hdr]
        for r in rows[1:]:
            if not r[0].strip():
                continue
            try:
                t_v = int(round(float(r[0])))
            except ValueError:
                continue
            for c_nm, cell in zip(cols[1:], r[1:]):
                if cell.strip():
                    try:
                        stella.setdefault(t_v, {})[c_nm] = float(cell)
                    except ValueError:
                        continue

    return comparar_mapa_stella(df_base, stella, ruta_stella.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Motor SSOT v2.2 (RK4 + Barlas + LTM + Sobol + 1D + Paridad Stella) — CONPES CTeI.")
    parser.add_argument("--proyecto", default="2026_conpes_ctei_colombia")
    parser.add_argument("--n-sobol", type=int, default=256)
    parser.add_argument("--comparar-stella", default="", help="Ruta opcional a .isdb o .csv de Stella (si se omite, autodetecta el .isdb en modelo_stella/).")
    args = parser.parse_args()

    p_script = Path(__file__).resolve()
    raiz = next((parent for parent in p_script.parents if (parent / "03_proyectos").exists()), Path.cwd())
    proy_dir = raiz / "03_proyectos" / args.proyecto
    csv_dir = proy_dir / "modelo_stella" / "corridas_csv"
    ssot_dir = proy_dir / "ssot"
    csv_dir.mkdir(parents=True, exist_ok=True)
    ssot_dir.mkdir(parents=True, exist_ok=True)

    params, fuente_p = cargar_params_calibrados_desde_ssot(proy_dir)
    print("=========================================================================")
    print(f" 🚀 METIS LAB — MOTOR SSOT v2.2 (11 STOCKS + SOBOL + BARRIDO 1D) | {args.proyecto}")
    print(f"    Parámetros calibrados: {fuente_p}")
    print("=========================================================================")

    # 1. Los 7 escenarios (2015–2050)
    trajs: Dict[str, pd.DataFrame] = {}
    res_rows = []
    for nom, ov in ESCENARIOS_PND.items():
        df, rmax = simular(params, ov, dt=0.25, tf=2050.0, metodo="RK4")
        df.to_csv(csv_dir / f"{nom}_v22.csv", index=False)
        trajs[nom] = df
        u = df.iloc[-1]
        res_rows.append({
            "Escenario": nom,
            "PIB_2050_M": round(float(u["gdp"]), 0),
            "Talento_2050": round(float(u["active_sti_talent"]), 1),
            "EmpInnov_2050": round(float(u["innovative_firms"]), 1),
            "SolucInd_2050": round(float(u["active_industrial_solutions"]), 1),
            "Presup_2050_M": round(float(u["rd_budget_stock"]), 1),
            "ExecRat_2050": round(float(u["budget_execution_ratio"]), 2),
            "ResCons": f"{rmax:.1e}",
        })
    df_res = pd.DataFrame(res_rows)

    # 2. Convergencia numérica RK4 a 3 pasos + Euler + Barlas unclamped
    conv_rows = []
    for sc in ("01_Status_Quo_CONPES", "05_PND_Integral_2026_2030"):
        r1 = float(simular(params, ESCENARIOS_PND[sc], dt=0.25, metodo="RK4")[0].iloc[-1]["active_industrial_solutions"])
        r2 = float(simular(params, ESCENARIOS_PND[sc], dt=0.125, metodo="RK4")[0].iloc[-1]["active_industrial_solutions"])
        r3 = float(simular(params, ESCENARIOS_PND[sc], dt=0.0625, metodo="RK4")[0].iloc[-1]["active_industrial_solutions"])
        eu = float(simular(params, ESCENARIOS_PND[sc], dt=0.25, metodo="Euler")[0].iloc[-1]["active_industrial_solutions"])
        conv_rows.append({
            "Escenario": sc,
            "RK4_0.25": round(r1, 3),
            "RK4_0.125": round(r2, 3),
            "RK4_0.0625": round(r3, 3),
            "RelDiff_RK4_pct": round(100.0 * abs(r1 - r3) / max(1e-6, r3), 4),
            "Euler_0.25": round(eu, 3),
        })
    df_conv = pd.DataFrame(conv_rows)

    u_bar1 = simular(params, ESCENARIOS_PND["06_Barlas_Extremo1_Colapso"], dt=0.25, aplicar_clamp=False)[0].iloc[-1]
    u_bar2 = simular(params, ESCENARIOS_PND["07_Barlas_Extremo2_Expansion"], dt=0.25, aplicar_clamp=False)[0].iloc[-1]

    # 3. LTM por fases en Status Quo
    df_sq = trajs["01_Status_Quo_CONPES"]
    df_ltm = pd.DataFrame([
        {
            "Fase": "Consolidación Histórica (2015–2026)",
            "R1_Fiscal_Transfer_pct": round(float(df_sq[df_sq["Time"] <= 2026]["ltm_r1_fiscal_pct"].mean()), 1),
            "R2_Talent_Loop_pct": round(float(df_sq[df_sq["Time"] <= 2026]["ltm_r2_talent_pct"].mean()), 1),
            "B1_BrainDrain_pct": round(float(df_sq[df_sq["Time"] <= 2026]["ltm_b1_braindrain_pct"].mean()), 1),
            "B2_Valley_Ration_pct": round(float(df_sq[df_sq["Time"] <= 2026]["ltm_b2_valley_ration_pct"].mean()), 1),
        },
        {
            "Fase": "Horizonte PND y Largo Plazo (2027–2050)",
            "R1_Fiscal_Transfer_pct": round(float(df_sq[df_sq["Time"] >= 2027]["ltm_r1_fiscal_pct"].mean()), 1),
            "R2_Talent_Loop_pct": round(float(df_sq[df_sq["Time"] >= 2027]["ltm_r2_talent_pct"].mean()), 1),
            "B1_BrainDrain_pct": round(float(df_sq[df_sq["Time"] >= 2027]["ltm_b1_braindrain_pct"].mean()), 1),
            "B2_Valley_Ration_pct": round(float(df_sq[df_sq["Time"] >= 2027]["ltm_b2_valley_ration_pct"].mean()), 1),
        },
    ])

    # 4. Sobol Panel A, Panel B y Barrido 1D
    print(f"1. [SOBOL PANEL A] 7 parámetros clave incluyendo retardo y presupuesto (n={args.n_sobol}, N={args.n_sobol*9})...")
    df_sob_a, meta_a = ejecutar_sobol_panel_a(params, n_base=args.n_sobol)
    df_sob_a.to_csv(ssot_dir / "sobol_panel_a_conpes_v22.csv", index=False)

    print("2. [SOBOL PANEL B] 3 Palancas PND [0,1]^3 (N=1,280, IC 95%)...")
    df_sob_b, n_tot_b = ejecutar_sobol_panel_b(params, n_pol=256)
    df_sob_b.to_csv(ssot_dir / "sobol_panel_b_conpes_v22.csv", index=False)

    print("3. [BARRIDO 1D FISCAL] Presupuesto/Ejecución aislados [0,1] (Pilares 2 y 3 en Baseline)...")
    df_1d = ejecutar_barrido_1d_fiscal(params)
    df_1d.to_csv(ssot_dir / "barrido_1d_fiscal_conpes_v22.csv", index=False)

    bloque_stella = "> No se encontró archivo `.isdb` en `modelo_stella/`."
    try:
        if args.comparar_stella:
            p_st = Path(args.comparar_stella).expanduser()
            if p_st.exists():
                bloque_stella = comparar_csv_stella(df_sq, p_st)
        else:
            p_isdb = localizar_isdb_mas_reciente(proy_dir / "modelo_stella")
            if p_isdb:
                print(f"4. [PARIDAD STELLA AUTO] Leyendo directamente SQLite `{p_isdb.name}`...")
                stella_map, etiq = leer_stella_isdb(p_isdb, csv_dir=csv_dir)
                bloque_stella = comparar_mapa_stella(df_sq, stella_map, etiq)
    except Exception as exc:
        bloque_stella = f"> [Aviso] Error leyendo resultados de Stella: {exc}"

    md = [
        f"# SSOT Verificado v2.2 (11 Stocks, Calibrado WB Colombia 2015–2024, Proyección 2050) — `{args.proyecto}`",
        f"**Generado:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} | **Parámetros:** {fuente_p} | **Metis Lab**\n",
        "## 1. Matriz Comparativa de 7 Escenarios Calibrados al Año 2050 (RK4, DT=0.25)",
        df_res.to_markdown(index=False),
        "\n## 2. Paridad Numérica Stella Architect vs. Testigo Python v2.2",
        bloque_stella,
        "\n## 3. Verificación Numérica: Convergencia RK4 (3 pasos) y Barlas Unclamped",
        f"- **Barlas Extremo 1 Unclamped:** `Soluc_2050={u_bar1['active_industrial_solutions']:.1f}`, `Talento_2050={u_bar1['active_sti_talent']:.1f}`, `EmpInnov_2050={u_bar1['innovative_firms']:.1f}`",
        f"- **Barlas Extremo 2 Unclamped:** `Soluc_2050={u_bar2['active_industrial_solutions']:.1f}`, `Talento_2050={u_bar2['active_sti_talent']:.1f}`, `EmpInnov_2050={u_bar2['innovative_firms']:.1f}`\n",
        df_conv.to_markdown(index=False),
        "\n## 4. Dominancia Cuantitativa de Lazos por Fase (LTM Proxy, Status Quo)",
        df_ltm.to_markdown(index=False),
        f"\n## 5. Sobol Panel A — Incertidumbre Paramétrica ±25% (n={args.n_sobol}, N={meta_a.get('n_corridas', 0)}, IC 95%)",
        f"- **Metadatos:** `{json.dumps(meta_a, ensure_ascii=False)}`\n",
        df_sob_a.to_markdown(index=False),
        f"\n## 6. Sobol Panel B — Separación Funcional de las 3 Palancas PND 2026–2030 (N={n_tot_b}, IC 95%)",
        df_sob_b.to_markdown(index=False),
        "\n## 7. Barrido 1D Contrafactual — Palanca 1 (Presupuesto/Ejecución Aislados [0, 1], Ecosistema y Retención en Baseline)",
        df_1d.to_markdown(index=False),
    ]
    ruta_rep = ssot_dir / "resumen_experimentos_conpes_v2_2050.md"
    ruta_rep.write_text("\n".join(md) + "\n", encoding="utf-8")

    print("\n✅ PARIDAD STELLA (.isdb) VS. PYTHON RK4:")
    print(bloque_stella)
    print("\n✅ SOBOL PANEL A (TOP 3 EN SOLUCIONES 2050):")
    print(df_sob_a[["Parametro", "S1_Soluc2050", "ST_Soluc2050_CI95", "ST_EmpInnov2050_CI95"]].head(4).to_string(index=False))
    print("\n✅ BARRIDO 1D FISCAL AISLADO:")
    print(df_1d[["Lever1_Level", "RD_GDP_Target_pct", "Talento_2050", "EmpInnov_2050", "SolucInd_2050", "Supera_20k_EmpInnov"]].to_string(index=False))
    print(f"\n✅ SSOT completo guardado en: {ruta_rep}")
    print("=========================================================================")


if __name__ == "__main__":
    main()