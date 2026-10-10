"""Plan Calculator data: negotiated rate x plan cost-sharing, for every DFW marketplace plan we can match to a rate file.

Usage: python calculator/oop.py YEAR --rates payer_rates_dfw_exchange.csv[.gz] [--tables DIR] [--out DIR]
Inputs:  DIR/dfw_exchange_plans_YEAR.csv and DIR/dfw_exchange_cost_sharing_YEAR.csv (from puf_to_dfw.py),
         the exchange-network rate file, calculator/data/dfw_facilities_npi.csv.
Outputs: exchange_oop_YEAR.csv (every plan x rate row with what the patient pays),
         calc_data_YEAR.json (the block embedded in the Plan Calculator page),
         unmatched_networks_YEAR.csv (plan networks with no rate-file match: review before publishing).

Same math as the 2026 steps 12 and 13 (deductible first, then copay or coinsurance, capped by the
out-of-pocket max and by the rate itself), with one fix: plans with separate medical and drug
deductibles now use the medical deductible (`deductible_for_calc`) instead of $0.
"""
import argparse
import json
import re
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
BAD_NPI = {"1639325806", "1467748251"}
# (insurer, CMS PUF network name, TiC network label, basis). Check new or renamed networks each year.
LINK = [
    ("Baylor Scott and White Health Plan", "BSW Premier HMO", "BSW Premier HMO", "exact name match"),
    ("Ambetter from Superior HealthPlan", "Value", "Exchange Value", 'name match (assumed: PUF "Value" = TiC "Exchange Value")'),
    ("Ambetter from Superior HealthPlan", "Premier", "Exchange Solutions|Exchange TX", "ASSUMED - verify with Ambetter"),
    ("Blue Cross and Blue Shield of Texas", "Blue Advantage", "BCBSTX Blue Advantage HMO", 'name match (BCBSTX TiC file "Blue-Advantage-HMO")'),
    ("Blue Cross and Blue Shield of Texas", "MyBlue Health", "BCBSTX MyBlue Health HMO", 'name match (BCBSTX TiC file "MyBlue-Health-HMO")'),
    ("Cigna Healthcare", "Texas Connect Network", "Cigna IFP Texas Connect", "plan IDs listed against this file in Cigna's index"),
    ("Oscar Insurance Company", "Individual Texas EPO", "Oscar Individual Texas EPO (064)", "CMS network URL networkId=064"),
    ("Oscar Insurance Company", "Individual Texas HMO", "Oscar Individual Texas HMO (059)", "CMS network URL networkId=059"),
    ("WellPoint", "Wellpoint Texas Essential and Pharmacy Base Network", "Wellpoint Texas Essential (TX_JBLXMED0001)", "Wellpoint plan search"),
    ("WellPoint", "Wellpoint Texas Essential and RX Choice Tiered Network", "Wellpoint Texas Essential (TX_JBLXMED0001)", "Wellpoint plan search"),
    ("WellPoint", "Wellpoint Texas Essential, Dental Prime, and RX Choice Tiered Network", "Wellpoint Texas Essential (TX_JBLXMED0001)", "Wellpoint plan search"),
    ("Molina Healthcare", "Molina Marketplace", "Molina Marketplace TX", "Molina TX index lists every plan against all files"),
]
NETKEY = {(a, b): c for a, b, c, _ in LINK}


def benefit(row):
    n = row.plain_name
    if "mammogram" in n.lower():
        return "Preventive Care/Screening/Immunization"
    if "MRI" in n or "CT " in n or "calcium" in n:
        return "Imaging (CT/PET Scans, MRIs)"
    if row.billing_code_type in ("MS-DRG", "DRG"):
        return "Inpatient Hospital Services (e.g., Hospital Stay)"
    if "delivery" in n.lower():
        return "Delivery and All Inpatient Services for Maternity Care"
    if row.billing_class == "professional" and row.facility_type != "ASC":
        return "Outpatient Surgery Physician/Surgical Services"
    return "Outpatient Facility Fee (e.g., Ambulatory Surgery Center)"


def num(s):
    return float(re.sub(r"[^\d.]", "", s)) if isinstance(s, str) and re.search(r"\d", s) else 0.0


def oop(rate, c, ded, moop):
    cp = float(c.copay_amount) if pd.notna(c.copay_amount) and c.copay_amount != "" else 0.0
    pc = float(c.coinsurance_pct) / 100 if pd.notna(c.coinsurance_pct) and c.coinsurance_pct != "" else 0.0
    d = min(rate, ded) if c.deductible_applies == "yes" else 0.0
    rest = rate - d
    return round(min(d + min(cp, rest) + pc * max(rest - cp, 0), moop, rate), 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("year", type=int)
    ap.add_argument("--rates", required=True)
    ap.add_argument("--tables", default=".")
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    T, O, y = Path(a.tables), Path(a.out), a.year
    O.mkdir(parents=True, exist_ok=True)
    plans = pd.read_csv(T / f"dfw_exchange_plans_{y}.csv", dtype=str, keep_default_na=False)
    cs = pd.read_csv(T / f"dfw_exchange_cost_sharing_{y}.csv", dtype=str, keep_default_na=False)
    rates = pd.read_csv(a.rates, dtype={"billing_code": str, "npi": str})
    fac = pd.read_csv(HERE / "data" / "dfw_facilities_npi.csv", dtype=str).drop_duplicates("npi").set_index("npi")

    std = plans[plans.variant == "standard"].copy()
    std["net_key"] = [NETKEY.get((i, n)) for i, n in zip(std.insurer, std.network_name)]
    um = std[std.net_key.isna()].groupby(["insurer", "network_name"]).plan_id.count().rename("plans").reset_index()
    um.to_csv(O / f"unmatched_networks_{y}.csv", index=False)

    r = rates[rates.negotiated_type.isin(["negotiated", "fee schedule"]) & (rates.negotiated_rate > 0) & ~rates.npi.isin(BAD_NPI)].copy()
    r["benefit"] = r.apply(benefit, axis=1)
    out = []
    for _, p in std.dropna(subset=["net_key"]).iterrows():
        ded, moop = num(p.deductible_for_calc), num(p.oop_max_individual)
        pr = r[r.network_name.str.contains(p.net_key.split("|")[0], regex=False)]
        pcs = cs[cs.plan_id == p.plan_id].drop_duplicates("benefit").set_index("benefit")
        for _, x in pr.iterrows():
            if x.benefit not in pcs.index:
                continue
            c = pcs.loc[x.benefit]
            out.append(dict(plan_id=p.plan_id, insurer=p.insurer, plan_name=p.plan_name, metal=p.metal, network=p.network_name,
                            deductible=ded, oop_max=moop, procedure=x.plain_name, billing_code=x.billing_code, provider=x.provider_name,
                            npi=x.npi, billing_class=x.billing_class, place_of_service_codes=x.service_code,
                            negotiated_rate=x.negotiated_rate, rate_type=x.negotiated_type, benefit_category=x.benefit,
                            plan_cost_share=f"{c.copay_raw} / {c.coinsurance_raw}",
                            you_pay_deductible_not_started=oop(x.negotiated_rate, c, ded, moop),
                            you_pay_deductible_met=oop(x.negotiated_rate, c, 0, moop),
                            rate_source=x.source_url, rate_file_date=x.last_updated_on))
    o = pd.DataFrame(out).drop_duplicates()
    o.to_csv(O / f"exchange_oop_{y}.csv", index=False)

    # ---- page data (same layout as the 2026 calc_data.json) ----
    o = o.astype(str)
    o = o[o.npi.isin(fac.index)]
    P = plans.drop_duplicates("plan_id").set_index("plan_id")
    BEN = sorted(o.benefit_category.unique()); bi = {b: i for i, b in enumerate(BEN)}
    procs = o[["billing_code", "procedure"]].drop_duplicates("billing_code").sort_values("procedure")
    PR = [[c, n] for c, n in procs.values]; pi = {c: i for i, (c, n) in enumerate(PR)}
    nets = o[["insurer", "network"]].drop_duplicates().sort_values(["insurer", "network"])
    NET = [list(x) for x in nets.values]; ni = {tuple(x): i for i, x in enumerate(NET)}
    SRC = sorted(o.rate_source.unique()); si = {s: i for i, s in enumerate(SRC)}
    npis = sorted(o.npi.unique()); vi = {n: i for i, n in enumerate(npis)}
    PROV = [[fac.loc[n, "name"], str(fac.loc[n, "city"]), fac.loc[n, "facility_type"], n] for n in npis]
    nn = lambda v: None if v in ("", "nan") else float(v)
    plan_rows = []
    for pid, g in o.groupby("plan_id"):
        f, p = g.iloc[0], P.loc[pid]
        c = cs[cs.plan_id == pid].drop_duplicates("benefit").set_index("benefit")
        csd = {bi[b]: [nn(c.loc[b, "copay_amount"]), nn(c.loc[b, "coinsurance_pct"]), 1 if c.loc[b, "deductible_applies"] == "yes" else 0,
                       c.loc[b, "copay_unit"], f"{c.loc[b, 'copay_raw']} / {c.loc[b, 'coinsurance_raw']}"] for b in BEN if b in c.index}
        plan_rows.append(dict(id=pid, n=f.plan_name.strip(), m=f.metal, d=float(f.deductible), x=float(f.oop_max), k=ni[(f.insurer, f.network)],
                              sbc=p.get("sbc_url", ""), cs=csd))
    R = o[["insurer", "network", "billing_code", "npi", "negotiated_rate", "rate_type", "benefit_category", "billing_class",
           "place_of_service_codes", "rate_source", "rate_file_date"]].drop_duplicates()
    rr = [[ni[(a_, b_)], pi[c_], vi[d_], round(float(e_), 2), f_, bi[g_], h_, (i_ if i_ != "nan" else ""), si[j_], k_]
          for a_, b_, c_, d_, e_, f_, g_, h_, i_, j_, k_ in R.values]
    D = dict(year=y, ben=BEN, proc=PR, net=NET, src=SRC, prov=PROV, plans=plan_rows, rates=rr)
    (O / f"calc_data_{y}.json").write_text(json.dumps(D, separators=(",", ":")))
    print(f"{y}: {len(std)} standard plans; {len(plan_rows)} in the calculator; {len(rr):,} rates; {len(PROV)} facilities")
    print(o.groupby("insurer").plan_id.nunique().to_string())
    if len(um):
        print("\nNetworks with no rate-file match (add to LINK in calculator/oop.py if a rate file exists):")
        print(um.to_string(index=False))


if __name__ == "__main__":
    main()
