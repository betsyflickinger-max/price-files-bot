"""Build the Plan Calculator's plan and cost-sharing tables for DFW from CMS's Marketplace PUFs.

Usage: python calculator/puf_to_dfw.py 2027 [--out DIR] [--puf DIR]
Downloads (or reads from --puf) plan-attributes, benefits-and-cost-sharing, service-area and network PUFs
from https://download.cms.gov/marketplace-puf/<year>/ and writes:
  dfw_exchange_plans_<year>.csv         one row per on-exchange plan variant sold in a DFW county
  dfw_exchange_cost_sharing_<year>.csv  one row per plan x benefit (the 21 benefits the calculator uses)
Same columns and parsing as the 2026 files (checked against them: see calculator/README.md).

Deductible note: when a plan has separate medical and drug deductibles, CMS leaves the combined
(TEHB) deductible as "Not Applicable" and puts the medical one in MEHB. The 2026 build read only the
combined column, so those plans were treated as having a $0 deductible. `deductible_for_calc` below
fixes that: it is the combined deductible, or the medical one when there is no combined deductible.
"""
import argparse
import io
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd
import requests

BASE = "https://download.cms.gov/marketplace-puf/{y}/{f}.zip"
FILES = ["plan-attributes-puf", "benefits-and-cost-sharing-puf", "service-area-puf", "network-puf"]
DFW = {"48085": "Collin", "48113": "Dallas", "48121": "Denton", "48139": "Ellis", "48221": "Hood", "48231": "Hunt",
       "48251": "Johnson", "48257": "Kaufman", "48367": "Parker", "48397": "Rockwall",
       "48439": "Tarrant", "48497": "Wise"}  # county FIPS codes, as the service-area PUF lists them
BENEFITS = ["Chemotherapy", "Delivery and All Inpatient Services for Maternity Care", "Durable Medical Equipment",
            "Emergency Room Services", "Emergency Transportation/Ambulance", "Home Health Care Services",
            "Imaging (CT/PET Scans, MRIs)", "Infusion Therapy", "Inpatient Hospital Services (e.g., Hospital Stay)",
            "Inpatient Physician and Surgical Services", "Laboratory Outpatient and Professional Services",
            "Mental/Behavioral Health Outpatient Services", "Outpatient Facility Fee (e.g., Ambulatory Surgery Center)",
            "Outpatient Rehabilitation Services", "Outpatient Surgery Physician/Surgical Services",
            "Preventive Care/Screening/Immunization", "Primary Care Visit to Treat an Injury or Illness",
            "Skilled Nursing Facility", "Specialist Visit", "Urgent Care Centers or Facilities", "X-rays and Diagnostic Imaging"]
NA = "Not Applicable"


def load(year, puf_dir):
    out = {}
    for f in FILES:
        local = Path(puf_dir) / f"{f}.csv" if puf_dir else None
        if local and local.exists():
            src = open(local, "rb").read()
        else:
            r = requests.get(BASE.format(y=year, f=f), timeout=600)
            if r.status_code == 404:
                sys.exit(f"CMS has not posted the {year} {f} yet ({BASE.format(y=year, f=f)} -> 404)")
            r.raise_for_status()
            z = zipfile.ZipFile(io.BytesIO(r.content))
            src = z.read([n for n in z.namelist() if n.endswith(".csv")][0])
        out[f] = pd.read_csv(io.BytesIO(src), dtype=str, encoding="utf-8-sig", keep_default_na=False)
    return out


def variant(v):
    m = re.match(r"(\d+)% AV Level Silver Plan", v)
    if m:
        return f"CSR {m.group(1)}%"
    return "standard" if "On Exchange" in v else None  # off-exchange, zero/limited cost sharing: not shown


def money(s):
    s = (s or "").strip()
    return "" if s in ("", NA) else s


def plans(p, year):
    pa = p["plan-attributes-puf"]
    pa = pa[(pa.StateCode == "TX") & (pa.MarketCoverage == "Individual") & (pa.DentalOnlyPlan == "No")].copy()
    pa["variant"] = pa.CSRVariationType.map(variant)
    pa = pa[pa.variant.notna()]
    sa = p["service-area-puf"]
    sa = sa[(sa.StateCode == "TX") & (sa.MarketCoverage == "Individual") & (sa.DentalOnlyPlan == "No")]
    whole = sa[sa.CoverEntireState == "Yes"][["IssuerId", "ServiceAreaId"]].drop_duplicates()
    whole = pd.concat([whole.assign(County=f) for f in DFW])
    cty = pd.concat([sa[["IssuerId", "ServiceAreaId", "County"]], whole])
    cty = cty.assign(c=cty.County.str.strip().str.zfill(5).map(DFW))
    cty = cty[cty.c.notna()].groupby(["IssuerId", "ServiceAreaId"]).c.apply(lambda s: "; ".join(sorted(set(s))))
    pa = pa.join(cty.rename("dfw_counties"), on=["IssuerId", "ServiceAreaId"])
    pa = pa[pa.dfw_counties.notna()]
    net = p["network-puf"]
    net = net[net.StateCode == "TX"].drop_duplicates(["IssuerId", "NetworkId"]).set_index(["IssuerId", "NetworkId"])
    pa = pa.join(net[["NetworkName", "NetworkURL"]], on=["IssuerId", "NetworkId"])
    t_ded, m_ded = pa.TEHBDedInnTier1Individual.map(money), pa.MEHBDedInnTier1Individual.map(money)
    out = pd.DataFrame(dict(
        plan_year=str(year), plan_id=pa.PlanId, insurer=pa.IssuerMarketPlaceMarketingName.str.strip(),
        plan_name=pa.PlanMarketingName, variant=pa.variant, metal=pa.MetalLevel, plan_type=pa.PlanType,
        network_name=pa.NetworkName, network_id=pa.NetworkId,
        deductible_individual=t_ded, oop_max_individual=pa.TEHBInnTier1IndividualMOOP.map(money),
        oop_max_family_per_person=pa.TEHBInnTier1FamilyPerPersonMOOP.map(money),
        med_deductible_individual_if_separate=m_ded, med_oop_max_individual_if_separate=pa.MEHBInnTier1IndividualMOOP.map(money),
        dfw_counties=pa.dfw_counties, sbc_url=pa.URLForSummaryofBenefitsCoverage, brochure_url=pa.PlanBrochure,
        network_url=pa.NetworkURL, referral_required=pa.IsReferralRequiredForSpecialist,
        source=f"CMS Marketplace PUF {year} (Plan Attributes, Network, Service Area)",
        deductible_for_calc=[t or m for t, m in zip(t_ded, m_ded)]))
    return out.sort_values("plan_id").reset_index(drop=True)


def num(s):
    m = re.search(r"[\d,]+(\.\d+)?", s)
    return float(m.group(0).replace(",", "")) if m else None


def parse(copay, coins):
    c = dict(copay_amount="", copay_unit="", copay_timing="", coinsurance_pct="", coinsurance_timing="")
    if copay and copay != NA:
        c["copay_amount"] = 0.0 if copay.startswith("No Charge") else num(copay)
        c["copay_unit"] = "per stay" if "per Stay" in copay else "per day" if "per Day" in copay else "per service"
        c["copay_timing"] = ("after_deductible" if "after deductible" in copay else
                             "with_deductible" if "with deductible" in copay else "no_deductible")
    if coins and coins != NA:
        c["coinsurance_pct"] = 0.0 if coins.startswith("No Charge") else num(coins)
        c["coinsurance_timing"] = "after_deductible" if "after deductible" in coins else "no_deductible"
    ded = "yes" if "after_deductible" in (c["copay_timing"], c["coinsurance_timing"]) or c["copay_timing"] == "with_deductible" else "no"
    cp, pc = c["copay_amount"] or 0.0, c["coinsurance_pct"] or 0.0
    kind = ("copay_plus_coinsurance" if cp > 0 and pc > 0 else "copay_only" if cp > 0 else
            "coinsurance_only" if pc > 0 else "no_charge")
    return {**c, "deductible_applies": ded, "cost_share_type": kind}


def cost_sharing(p, plan_ids):
    b = p["benefits-and-cost-sharing-puf"]
    b = b[b.PlanId.isin(plan_ids) & b.BenefitName.isin(BENEFITS)]
    rows = []
    for r in b.itertuples(index=False):
        rows.append(dict(plan_id=r.PlanId, benefit=r.BenefitName, is_covered=r.IsCovered, copay_raw=r.CopayInnTier1.strip(),
                         coinsurance_raw=r.CoinsInnTier1.strip(), **parse(r.CopayInnTier1.strip(), r.CoinsInnTier1.strip()),
                         notes=r.Explanation.strip()))
    cols = ["plan_id", "benefit", "is_covered", "copay_raw", "coinsurance_raw", "copay_amount", "copay_unit", "copay_timing",
            "coinsurance_pct", "coinsurance_timing", "deductible_applies", "cost_share_type", "notes"]
    return pd.DataFrame(rows, columns=cols).sort_values(["plan_id", "benefit"]).reset_index(drop=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("year", type=int)
    ap.add_argument("--out", default=".")
    ap.add_argument("--puf", default="", help="folder with already-unzipped PUF csvs")
    a = ap.parse_args()
    p = load(a.year, a.puf)
    pl = plans(p, a.year)
    cs = cost_sharing(p, set(pl.plan_id))
    Path(a.out).mkdir(parents=True, exist_ok=True)
    pl.to_csv(Path(a.out) / f"dfw_exchange_plans_{a.year}.csv", index=False)
    cs.to_csv(Path(a.out) / f"dfw_exchange_cost_sharing_{a.year}.csv", index=False)
    print(f"{a.year}: {len(pl)} plan variants ({(pl.variant == 'standard').sum()} standard) from "
          f"{pl.insurer.nunique()} insurers; {len(cs)} cost-sharing rows")
    print(pl[pl.variant == "standard"].groupby("insurer").size().to_string())
