"""
Plain-language explanations for every term the dashboard shows.

Written for someone who runs a dispensary, not someone who does analytics.
Rules followed here:

  - define the thing, do not restate the jargon
  - say why it matters, not just what it is
  - give the reading rule where there is one ("above 1.2 means...")
  - no term inside a definition that is not itself defined

Used three ways: `help=` tooltips on metrics and table columns, hover
markers beside headings, and the glossary panel.
"""

from __future__ import annotations

GLOSSARY: dict[str, str] = {

    # --- money and volume ------------------------------------------------
    "net sales":
        "Money taken after discounts and loyalty redemptions are subtracted, "
        "before tax. This is the number to compare across weeks — it is what "
        "the business actually received.",

    "basket":
        "One transaction. One customer, one checkout. Also called a ticket. "
        "If someone buys three things at once, that is one basket with three "
        "lines.",

    "average basket":
        "Net sales divided by the number of baskets — what a typical customer "
        "spends per visit. If revenue falls but average basket holds, you have "
        "a traffic problem, not a spending problem.",

    "gross margin":
        "What is left after subtracting what the product cost you. Margin % is "
        "that figure as a share of the sale price. It does not account for "
        "rent, wages or other overhead.",

    # --- the metrics that do the work -----------------------------------
    "$/100 baskets":
        "How much this category earns for every 100 transactions. The most "
        "useful number here, because it separates the category from the "
        "traffic. If footfall drops 20% and Flower revenue drops 20%, Flower "
        "is fine — this metric would be flat. Raw revenue would look alarming.",

    "penetration":
        "Out of every 100 transactions, how many contained this category. "
        "Reach, not revenue. A category can hold its revenue while its "
        "penetration falls — that means fewer people buying more each, which "
        "is a more fragile position than the revenue line suggests.",

    "units":
        "Number of individual items sold, not transactions. A five-pack of "
        "pre-rolls sold once is one unit.",

    # --- change notation --------------------------------------------------
    "Δ WoW":
        "Change versus the previous week, as a percentage. Δ is shorthand for "
        "'change in'.",

    "pp":
        "Percentage points — the gap between two percentages. Going from 20% "
        "to 23% is a rise of 3 percentage points, not 3%. Used to avoid the "
        "ambiguity of 'percent of a percent'.",

    # --- statistical machinery, explained without statistics -------------
    "control limits":
        "The normal range for this category, worked out from the last 13 "
        "weeks. Roughly 95% of ordinary weeks land inside the shaded band. A "
        "point outside it is unusual enough to be worth a look — not proof "
        "anything is wrong, just a prompt to check.",

    "baseline":
        "The average of the last 13 weeks. The dotted line the shaded band is "
        "built around.",

    "run rule":
        "Seven weeks in a row on the same side of the baseline, or six weeks "
        "moving the same direction. This catches slow erosion — a category "
        "losing a little every week never triggers an alarm on any single "
        "week, but it will have lost a fifth of its reach by the end of a "
        "quarter. This is usually the more important signal.",

    # --- channels ---------------------------------------------------------
    "channel":
        "How the sale happened: In-Store at a register, Non-Stop for prepaid "
        "online pickup, or Delivery.",

    "channel index":
        "Whether a category sells more or less through a given channel than it "
        "does overall. 100 means exactly in line. Above 115 means it "
        "over-indexes — customers of that channel reach for it more than "
        "average, so protect the stock and give it menu position. Below 85 "
        "means the opposite, and is often a menu placement problem rather "
        "than genuinely weak demand.",

    # --- basket relationships --------------------------------------------
    "co-purchase lift":
        "Whether two categories get bought together more or less often than "
        "chance would predict. Above 1.3 means genuine affinity — put them "
        "near each other. Below 0.7 means customers pick one or the other. "
        "Around 1.0 means no relationship.\n\n"
        "Only baskets already containing two or more categories are counted. "
        "Over half of all baskets hold a single item, and including those "
        "makes every pair look like a substitute, which tells you nothing.",

    "substitutes":
        "Two categories customers choose between rather than buying together. "
        "Worth knowing before you cut one — the demand may just move rather "
        "than disappear.",

    "affinity":
        "Two categories customers tend to buy on the same visit. Candidates "
        "for adjacent placement, bundles, or an upsell prompt.",

    # --- inventory --------------------------------------------------------
    "SSI":
        "Space-to-Sales Index. Compares a category's share of revenue against "
        "its share of the money tied up in stock.\n\n"
        "Above 1.2 — earning more than its share of your capital. A case for "
        "expanding it.\n"
        "0.8 to 1.2 — balanced.\n"
        "Below 0.8 — capital sitting in stock that is not pulling its weight.",

    "days supply":
        "At the current rate of sale, how many days until the shelf is empty. "
        "Very high numbers mean cash tied up in slow-moving product; very low "
        "numbers mean you are at risk of running out.",

    "inventory at cost":
        "What you paid for the stock currently on hand — not what it will sell "
        "for. This is the money currently tied up.",

    "SKUs":
        "Distinct products. Two strains of the same brand and size are two "
        "SKUs. A high SKU count against low revenue means the range is spread "
        "thin.",

    "sellable stock":
        "Inventory on the sales floor, in the vault, or in the day vault. "
        "Excludes quarantine, samples, destruction and display units.",
}


# Longer notes attached to whole sections rather than single terms.
SECTIONS: dict[str, str] = {

    "revenue_traffic":
        "Two things at once: bars are money, the line is number of "
        "transactions. When they move together, it is a traffic story. When "
        "they separate, customers are spending differently — and that is a "
        "different problem with a different fix.",

    "per100":
        "Each line is one category's earnings per 100 transactions. Because "
        "the traffic is divided out, a line falling here means that category "
        "genuinely weakened — it is not just that fewer people came in.",

    "control_chart":
        "The shaded band is the normal range based on the last 13 weeks. The "
        "green line is what actually happened. Circled points fell outside "
        "normal.\n\n"
        "One point outside the band is worth a look. Seven consecutive weeks "
        "on one side matters more, even if every point stays inside — that is "
        "a trend rather than a bad week.",

    "channel_grid":
        "Green means the category sells disproportionately well in that "
        "channel; red means disproportionately poorly. 100 is neutral. Red in "
        "an online channel is often a menu placement issue rather than weak "
        "demand — worth checking position and imagery before concluding "
        "customers do not want it.",

    "scorecard":
        "The week just ended, by category. Read $/100 baskets and penetration "
        "together: revenue tells you what the category sells, penetration "
        "tells you how many customers it reaches. A category holding revenue "
        "while penetration slides is more fragile than it looks.",

    "alerts":
        "Generated automatically by comparing this week against the last 13. "
        "These are prompts to investigate, not conclusions. Categories under "
        "1% of revenue are excluded — the numbers are too small to be "
        "meaningful.",

    "lift":
        "Which categories get bought together, and which get chosen between. "
        "Useful for menu layout, bundles and deciding whether cutting a "
        "category would move demand elsewhere or simply lose it.",

    "inventory":
        "Whether the money tied up in each category matches what it earns. "
        "The most common finding is a long tail of products with many SKUs, "
        "high stock and little revenue.",
}


_LOOKUP = {k.lower(): v for k, v in GLOSSARY.items()}


def tip(term: str) -> str:
    """Definition for a term, for use in a help= parameter.

    Case-insensitive, and tolerant of the Greek delta so "Δ WoW" and
    "delta wow" both resolve.
    """
    k = term.strip().lower()
    if k in _LOOKUP:
        return _LOOKUP[k]
    k2 = k.replace("Δ", "delta").replace("δ", "delta")
    for cand, val in _LOOKUP.items():
        if cand.replace("Δ", "delta").replace("δ", "delta") == k2:
            return val
    return ""


def marker(term: str, label: str | None = None) -> str:
    """An inline hover marker. Renders as the label with a dotted underline;
    the definition appears as a native browser tooltip on hover."""
    text = tip(term)
    if not text:
        return label or term
    safe = text.replace('"', "&quot;").replace("\n", " ")
    return (f'<span class="gloss" title="{safe}">{label or term}'
            f'<span class="gloss-mark">?</span></span>')


def section_note(key: str) -> str:
    return SECTIONS.get(key, "")
