#!/usr/bin/env python3
"""
This script converts cannabis sales and inventory Excel files into the JSON
format required by the Brand PPI + Sell‑Through + Revenue CRM dashboard.

Usage:
  python excel_to_json_converter.py \
      --inventory "Inventory Receive Costing report 1_1_2025-9_13_2025.xlsx" \
      --sales "Detailed Sales Breakdown by Product 1_1_2025-3_31_2025.xlsx" \
      --sales "Detailed Sales Breakdown by Product 4_1_2025-6_30_2025.xlsx" \
      --sales "Detailed Sales Breakdown by Product 7_1_2025-9_13_2025.xlsx" \
      --vendor_map "consolidated_brand_vendor.csv" \
      --output brand_ppi_crm_data.json

The script reads the inventory report and sales breakdowns, normalizes vendor
and brand names using an optional mapping, computes weighted average
wholesale cost (PPI index), sell‑through, revenue share and the Brand
Efficiency Index (BEI), and writes a JSON file suitable for the dashboard.

The PPI index is calculated per brand as (avg_unit_cost / overall_avg_cost)
multiplied by 100 so that values around 100 represent the average cost.
Sell‑through is computed as units_sold / (units_sold + units_received),
falling back to 0 if no inventory information exists.  Revenue share is
computed against the total net sales across all brands.  BEI is derived
client‑side in the dashboard if not provided here.

"""

import argparse
import json
import math
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd


def normalize_name(name: str) -> str:
    """Return a simplified version of a vendor/brand name for comparison.

    Normalization removes punctuation, whitespace, common company suffixes,
    converts to lowercase and strips trailing 's'.  This makes it easier to
    match vendors with their corresponding brand names.
    """
    if not isinstance(name, str):
        return ""
    name = name.lower().strip()
    # Remove punctuation and whitespace
    name = re.sub(r"[\s\-\.,]+", "", name)
    # Remove common legal entity suffixes
    suffixes = ["llc", "inc", "incorporated", "corp", "corporation",
                "company", "co", "dba", "limited", "ltd", "pllc",
                "plc", "group", "holdings", "farms", "farm"]
    for suf in suffixes:
        if name.endswith(suf):
            name = name[: -len(suf)]
    # Remove trailing "s" if present after suffix removal
    if name.endswith("s"):
        name = name[:-1]
    return name


def load_vendor_mapping(csv_path: Optional[str]) -> Dict[str, str]:
    """Load a mapping of vendor identifiers to canonical brand names.

    The CSV should contain at least two columns: Vendor and Brand.  The
    mapping will normalize both columns and map the normalized vendor name
    to the canonical (original) brand name.  If no mapping file is
    provided or it cannot be read, an empty mapping is returned.
    """
    mapping: Dict[str, str] = {}
    if not csv_path or not os.path.exists(csv_path):
        return mapping
    try:
        df = pd.read_csv(csv_path)
        vendor_col = None
        brand_col = None
        # Find appropriate columns regardless of case
        for col in df.columns:
            col_lower = col.lower()
            if vendor_col is None and 'vendor' in col_lower:
                vendor_col = col
            if brand_col is None and 'brand' in col_lower:
                brand_col = col
        if vendor_col is None or brand_col is None:
            return mapping
        for _, row in df.iterrows():
            vendor = str(row[vendor_col]).strip()
            brand = str(row[brand_col]).strip()
            if vendor and brand:
                n_vendor = normalize_name(vendor)
                mapping[n_vendor] = brand
    except Exception:
        pass
    return mapping


def load_inventory(inventory_path: str, mapping: Dict[str, str]) -> Dict[str, Tuple[float, float]]:
    """Read the inventory Excel file and aggregate cost data per brand.

    Returns a dictionary keyed by canonical brand name with values
    (total_cost, total_quantity).  The average unit cost can be computed
    as total_cost / total_quantity for each brand.
    """
    inventory_data: Dict[str, Tuple[float, float]] = {}
    if not inventory_path or not os.path.exists(inventory_path):
        return inventory_data
    try:
        # Skip the first four metadata rows based on previous file structure
        inv_df = pd.read_excel(inventory_path, skiprows=4, dtype=str)
        # Identify columns regardless of case
        cols = {c.lower(): c for c in inv_df.columns}
        vendor_col = cols.get('vendor name') or cols.get('vendor')
        qty_col = cols.get('quantity') or cols.get('qty') or cols.get('quantity received')
        cost_col = cols.get('inventory cost') or cols.get('cost') or cols.get('inventory cost ($)')
        if not vendor_col or not qty_col or not cost_col:
            return inventory_data
        # Convert numeric columns
        inv_df[qty_col] = pd.to_numeric(inv_df[qty_col], errors='coerce')
        inv_df[cost_col] = pd.to_numeric(inv_df[cost_col], errors='coerce')
        for _, row in inv_df.iterrows():
            vendor = row.get(vendor_col)
            qty = row.get(qty_col)
            cost = row.get(cost_col)
            if isinstance(vendor, str) and not pd.isna(qty) and not pd.isna(cost):
                brand_canonical = mapping.get(normalize_name(vendor), vendor)
                # Accumulate cost and quantity
                total_cost, total_qty = inventory_data.get(brand_canonical, (0.0, 0.0))
                total_cost += float(cost)
                total_qty += float(qty)
                inventory_data[brand_canonical] = (total_cost, total_qty)
    except Exception:
        return inventory_data
    return inventory_data


def load_sales(sales_paths: List[str], mapping: Dict[str, str]) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, Dict[str, float]]]:
    """Aggregate sales data across multiple Excel files.

    Returns three dictionaries keyed by canonical brand name:
      - revenue_map: total net sales per brand
      - units_map: total quantity sold per brand
      - category_revenue: mapping of category to revenue per brand
    """
    revenue_map: Dict[str, float] = {}
    units_map: Dict[str, float] = {}
    category_revenue: Dict[str, Dict[str, float]] = {}
    for sales_path in sales_paths:
        if not sales_path or not os.path.exists(sales_path):
            continue
        try:
            df = pd.read_excel(sales_path, skiprows=4, dtype=str)
            # Identify columns
            cols = {c.lower(): c for c in df.columns}
            brand_col = cols.get('brand name') or cols.get('brand')
            category_col = cols.get('category')
            qty_col = cols.get('quantity sold') or cols.get('units sold') or cols.get('quantity')
            revenue_col = cols.get('net sales') or cols.get('revenue')
            # Convert numeric
            if qty_col:
                df[qty_col] = pd.to_numeric(df[qty_col], errors='coerce')
            if revenue_col:
                df[revenue_col] = pd.to_numeric(df[revenue_col], errors='coerce')
            for _, row in df.iterrows():
                brand = row.get(brand_col)
                qty = row.get(qty_col)
                revenue = row.get(revenue_col)
                category = row.get(category_col)
                if isinstance(brand, str) and not pd.isna(qty) and not pd.isna(revenue):
                    brand_canonical = mapping.get(normalize_name(brand), brand)
                    revenue_map[brand_canonical] = revenue_map.get(brand_canonical, 0.0) + float(revenue)
                    units_map[brand_canonical] = units_map.get(brand_canonical, 0.0) + float(qty)
                    if isinstance(category, str):
                        category_dict = category_revenue.setdefault(brand_canonical, {})
                        category_dict[category] = category_dict.get(category, 0.0) + float(revenue)
        except Exception:
            continue
    return revenue_map, units_map, category_revenue


def compute_metrics(
    inventory_data: Dict[str, Tuple[float, float]],
    revenue_map: Dict[str, float],
    units_map: Dict[str, float],
    category_revenue: Dict[str, Dict[str, float]]
    ) -> Dict[str, Dict]:
    """Calculate average unit cost, PPI index, sell‑through, category mix and history per brand."""
    brand_data: Dict[str, Dict] = {}
    # Calculate average cost per brand and overall average cost
    costs = {}
    for brand, (total_cost, total_qty) in inventory_data.items():
        if total_qty > 0:
            costs[brand] = total_cost / total_qty
    # If cost data is missing for some brands, we may infer cost from sales costs if available in units_map? For now leave missing.
    overall_avg_cost = sum(costs.values()) / len(costs) if costs else None
    # Build brand entries
    all_brands = set(revenue_map.keys()) | set(units_map.keys()) | set(inventory_data.keys())
    for brand in all_brands:
        revenue = revenue_map.get(brand, 0.0)
        units_sold = units_map.get(brand, 0.0)
        total_cost, total_qty = inventory_data.get(brand, (0.0, 0.0))
        # compute avg unit cost: prefer inventory
        avg_unit_cost = None
        if total_qty > 0:
            avg_unit_cost = total_cost / total_qty
        # compute PPI index
        if avg_unit_cost is not None and overall_avg_cost and overall_avg_cost > 0:
            ppi_index = (avg_unit_cost / overall_avg_cost) * 100
        else:
            # fallback: use 0 if no cost information
            ppi_index = 0.0
        # compute sell-through: units sold / (units sold + units received)
        units_received = total_qty
        if units_received + units_sold > 0:
            sell_through = units_sold / (units_sold + units_received)
        else:
            sell_through = 0.0
        # compute category mix
        cat_rev = category_revenue.get(brand, {})
        cat_total = sum(cat_rev.values())
        cat_mix = {}
        if cat_total > 0:
            for cat, rev in cat_rev.items():
                cat_mix[cat] = rev / cat_total
        brand_data[brand] = {
            'brand': brand,
            'revenue': revenue,
            'units': units_sold,
            'avg_unit_cost': avg_unit_cost,
            'ppi_index': ppi_index,
            'sell_through_pct': sell_through,
            'category_mix': cat_mix,
            'history': []  # history can be populated if monthly grouping is desired
        }
    return brand_data


def build_json(
    brand_data: Dict[str, Dict],
    meta_notes: str = '',
    geography: str = 'NY Adult‑Use',
    currency: str = 'USD'
    ) -> Dict:
    """Assemble the final JSON structure for the CRM dashboard."""
    total_revenue = sum(b['revenue'] for b in brand_data.values())
    total_units = sum(b['units'] for b in brand_data.values())
    # Assemble brands list
    brands_list: List[Dict] = []
    for b in brand_data.values():
        # compute revenue share and BEI here to preserve compatibility
        share = (b['revenue'] / total_revenue) if total_revenue > 0 else 0
        b['revenue_share'] = share
        # BEI will be computed client-side if not provided
        # Append brand entry
        brands_list.append(b)
    # Sort brands by revenue descending for convenience
    brands_list.sort(key=lambda x: x['revenue'], reverse=True)
    json_data = {
        'meta': {
            'title': 'Brand PPI + Sell‑Through + Revenue CRM',
            'as_of': pd.Timestamp.now().strftime('%Y-%m-%d'),
            'currency': currency,
            'geography': geography,
            'notes': meta_notes
        },
        'kpis': {
            'total_revenue': total_revenue,
            'total_units': total_units
        },
        'brands': brands_list
    }
    return json_data


def main():
    parser = argparse.ArgumentParser(description='Convert cannabis sales & inventory Excel files to CRM JSON.')
    parser.add_argument('--inventory', help='Path to inventory Excel file', required=True)
    parser.add_argument('--sales', action='append', help='Path to a sales Excel file (may be specified multiple times)', required=True)
    parser.add_argument('--vendor_map', help='CSV mapping of vendor to canonical brand', default=None)
    parser.add_argument('--output', help='Path to write JSON output file', required=True)
    parser.add_argument('--notes', help='Optional notes for meta section', default='')
    parser.add_argument('--geography', help='Geography for meta section', default='NY Adult‑Use')
    parser.add_argument('--currency', help='Currency code', default='USD')
    args = parser.parse_args()

    mapping = load_vendor_mapping(args.vendor_map)
    inventory_data = load_inventory(args.inventory, mapping)
    revenue_map, units_map, category_revenue = load_sales(args.sales, mapping)
    brand_data = compute_metrics(inventory_data, revenue_map, units_map, category_revenue)
    json_data = build_json(brand_data, meta_notes=args.notes, geography=args.geography, currency=args.currency)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(brand_data)} brands to {args.output}")


if __name__ == '__main__':
    main()