#!/usr/bin/env python3
"""
Sales Report Processor

This script reads a sales report exported from your dispensary system and
produces an HTML summary showing, for each category, the top and bottom three
products by quantity sold. If the category is "Flower", the summary also breaks
things down by weight group (ounce, 14gs, 7gs and 3.5g).

Usage:
    python sales_report_processor.py /path/to/report.xlsx [output.html]

The first argument is the path to the Excel report. The second argument is
optional and specifies where to write the output HTML file. If omitted, the
script writes "report_summary.html" in the current directory.

The script should work with reports that include extra preamble rows before
column headers. It searches for the header row by looking for a column named
"ProductName" (case‑insensitive, ignoring spaces). It then uses the
"Category", "ProductName", "QuantitySold", and "NetSales" (or "GrossSales")
columns to compute the summary. If "NetSales" is unavailable, it falls back to
"GrossSales".

Requires: pandas
"""

import sys
import re
import pandas as pd


def find_header_row(df):
    """Return the index of the header row (containing ProductName)."""
    for i, row in df.iterrows():
        for cell in row:
            if pd.isnull(cell):
                continue
            normalized = str(cell).replace(" ", "").lower()
            if normalized == "productname":
                return i
    return None


def resolve_key(header_row, possible_names):
    """Find a matching column name from possible_names, ignoring spaces and case."""
    for target in possible_names:
        target_norm = target.replace(" ", "").lower()
        for h in header_row:
            if isinstance(h, float) or pd.isnull(h):
                continue
            if str(h).replace(" ", "").lower() == target_norm:
                return h
    return None


def summarize_report(filepath):
    raw_df = pd.read_excel(filepath, sheet_name=None, header=None)
    # Choose sheet named "Report" if present; else first sheet
    sheetname = 'Report' if 'Report' in raw_df else list(raw_df.keys())[0]
    df = raw_df[sheetname]
    header_idx = find_header_row(df)
    if header_idx is None:
        raise ValueError("Could not locate header row containing 'ProductName'")
    header_row = df.iloc[header_idx].tolist()
    data = df.iloc[header_idx + 1:].copy()
    data.columns = header_row
    # identify required keys
    cat_key = resolve_key(header_row, ['Category'])
    product_key = resolve_key(header_row, ['ProductName', 'Product Name'])
    qty_key = resolve_key(header_row, ['QuantitySold', 'Quantity Sold'])
    sales_key = resolve_key(header_row, ['NetSales', 'Net Sales', 'GrossSales', 'Gross Sales'])
    if not all([cat_key, product_key, qty_key, sales_key]):
        missing = [name for name, key in [('Category',cat_key),('ProductName',product_key),('QuantitySold',qty_key),('NetSales/GrossSales',sales_key)] if key is None]
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    # drop rows missing required values
    data = data[data[cat_key].notna() & data[product_key].notna()]
    data[qty_key] = pd.to_numeric(data[qty_key], errors='coerce').fillna(0)
    data[sales_key] = pd.to_numeric(data[sales_key], errors='coerce').fillna(0)
    summary = data.groupby([cat_key, product_key]).agg({qty_key:'sum', sales_key:'sum'}).reset_index()
    # compute top/bottom per category
    categories = {}
    for cat in summary[cat_key].dropna().unique():
        cat_df = summary[summary[cat_key] == cat].sort_values(by=qty_key, ascending=False)
        categories[cat] = {
            'top': cat_df.head(3),
            'bottom': cat_df.tail(3)
        }
    # flower breakdown
    def get_group(name):
        name = str(name).lower()
        if re.search(r'(28g|ounce|oz)', name): return 'ounce'
        if re.search(r'(14g|half ounce|half oz)', name): return '14gs'
        if re.search(r'(7g|quarter)', name): return '7gs'
        if re.search(r'(3\.5g|eighth)', name): return '3.5g'
        return 'other'
    flower_groups = {}
    if 'Flower' in categories:
        flower_df = summary[summary[cat_key] == 'Flower'].copy()
        flower_df['group'] = flower_df[product_key].apply(get_group)
        for g in ['ounce','14gs','7gs','3.5g']:
            gdf = flower_df[flower_df['group']==g].sort_values(by=qty_key, ascending=False)
            flower_groups[g] = {
                'top': gdf.head(3),
                'bottom': gdf.tail(3)
            }
    return categories, flower_groups, { 'cat_key': cat_key, 'product_key': product_key, 'qty_key': qty_key, 'sales_key': sales_key }


def generate_html(categories, flower_groups, keys):
    cat_key = keys['cat_key']; product_key = keys['product_key']; qty_key = keys['qty_key']; sales_key = keys['sales_key']
    parts = ["<html><head><meta charset='utf-8'>\n"
             "<style>body{font-family:Arial,Helvetica,sans-serif;background:#f7f9fb;color:#333;padding:20px;}"
             "h1{color:#222;}h2{color:#003366;border-bottom:2px solid #eee;padding-bottom:4px;}"
             "table{width:100%;border-collapse:collapse;margin-top:4px;font-size:0.9rem;}"
             "th,td{padding:6px;border-bottom:1px solid #eaeaea;text-align:left;}"
             "th{background:#f0f4f8;color:#555;font-weight:bold;}tr:nth-child(even){background:#fafafa;}"
             ".section{background:#fff;padding:10px 15px;margin-bottom:20px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.05);}"
             ".subheading{font-weight:bold;color:#006699;margin-top:10px;}"
             "</style></head><body><h1>Product Sales Report Summary</h1>"]
    for cat, data in categories.items():
        parts.append(f"<div class='section'><h2>{cat}</h2>")
        # top
        parts.append("<div class='subheading'>Top 3 Products</div>")
        parts.append("<table><thead><tr><th>Product Name</th><th>Total Sold</th><th>Total Sales</th></tr></thead><tbody>")
        for _, row in data['top'].iterrows():
            parts.append(f"<tr><td>{row[product_key]}</td><td>{int(row[qty_key])}</td><td>{row[sales_key]:.2f}</td></tr>")
        parts.append("</tbody></table>")
        # bottom
        parts.append("<div class='subheading'>Bottom 3 Products</div>")
        parts.append("<table><thead><tr><th>Product Name</th><th>Total Sold</th><th>Total Sales</th></tr></thead><tbody>")
        for _, row in data['bottom'].iterrows():
            parts.append(f"<tr><td>{row[product_key]}</td><td>{int(row[qty_key])}</td><td>{row[sales_key]:.2f}</td></tr>")
        parts.append("</tbody></table>")
        # flower breakdown
        if cat == 'Flower' and flower_groups:
            for g, gdata in flower_groups.items():
                # top group
                parts.append(f"<div class='subheading'>{g.upper()} - Top 3</div>")
                parts.append("<table><thead><tr><th>Product Name</th><th>Total Sold</th><th>Total Sales</th></tr></thead><tbody>")
                for _, row in gdata['top'].iterrows():
                    parts.append(f"<tr><td>{row[product_key]}</td><td>{int(row[qty_key])}</td><td>{row[sales_key]:.2f}</td></tr>")
                parts.append("</tbody></table>")
                # bottom group
                parts.append(f"<div class='subheading'>{g.upper()} - Bottom 3</div>")
                parts.append("<table><thead><tr><th>Product Name</th><th>Total Sold</th><th>Total Sales</th></tr></thead><tbody>")
                for _, row in gdata['bottom'].iterrows():
                    parts.append(f"<tr><td>{row[product_key]}</td><td>{int(row[qty_key])}</td><td>{row[sales_key]:.2f}</td></tr>")
                parts.append("</tbody></table>")
        parts.append("</div>")
    parts.append("</body></html>")
    return ''.join(parts)


def main():
    if len(sys.argv) < 2:
        print("Usage: python sales_report_processor.py /path/to/report.xlsx [output.html]")
        sys.exit(1)
    infile = sys.argv[1]
    outfile = sys.argv[2] if len(sys.argv) > 2 else 'report_summary.html'
    categories, flower_groups, keys = summarize_report(infile)
    html = generate_html(categories, flower_groups, keys)
    with open(outfile, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'Written summary to {outfile}')

if __name__ == '__main__':
    main()
