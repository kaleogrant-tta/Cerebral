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

    "own lift":
        "How the hosting store did on the event day against its own normal for that weekday — the same weekday within six weeks either side, skipping days next to other events. +20% means a fifth better than a typical Thursday.",

    "other stores":
        "How the stores that did NOT host the event did against their own normal, the same day. This is the citywide background — weather, holidays, a transit strike. Blank where there is no control group.",

    "effect":
        "Own lift minus other stores. What is left once the citywide background is removed, and the number to judge a single-store event by.",

    "lift vs normal":
        "For events with no control group (off-site, or running at every store): how sales compared with the same weekday nearby. Nothing can be subtracted, so this still includes whatever else was happening in the city that day. Read it as an upper bound.",

    "controls":
        "How many comparable days were averaged to build the baseline. Three is the minimum and one odd day can swing it; eight or more is solid.",

    "n small":
        "Fewer than ten events in the group. With that few, a confident-looking range can appear by accident. Read these rows as a description of what happened, not as evidence of a pattern.",

    "95% interval":
        "The range the true effect very likely sits in. If it includes zero, no effect has been detected — the data cannot tell a real change from ordinary day-to-day noise.",

    "incremental new":
        "First-time buyers on the event day at the hosting store, over what that weekday would normally bring in, after subtracting the other stores. Can be a fraction because the baseline is an average. This is the divisor for cost per net-new customer.",

    "net cost":
        "What the event cost TTA: its own direct spend, plus an even share of any budget line shared with other events, minus anything a brand partner reimbursed. Marketing's corrected figure, not Airtable's rollup.",

    "unrecorded":
        "No budget line is linked to this event. That is not the same as free — some of these were free and some were never written down, and the data cannot tell them apart. Shown, never divided.",

    "$ / net-new (lift)":
        "Net cost divided by incremental new customers from the lift model. Single-store events only, and only with enough evidence — below that a dollar figure is more luck than measurement.",

    "$ / net-new (signups)":
        "Net cost divided by sign-ups whose first ever TTA purchase came on or after the event. The only usable per-customer figure for off-site events, and only where a sign-up list is mapped.",

    "$ / net-new customer (lift)":
        "Net cost divided by incremental new customers from the lift model, pooled across every event in the group. Pooling cancels most of the night-to-night noise; this is the figure to quote.",

    "$ / signup":
        "Net cost divided by everyone on the sign-up list. A real number but a weak one: it rewards a long list, not a good event.",

    "signups":
        "People on the event's Alpine IQ list — who registered, not who came. There is no reliable check-in data, so no rate on this tab is per attendee.",

    "net-new of signups":
        "Sign-ups whose first ever TTA purchase was on or after the event. Customers the event created.",

    "why blank":
        "Why a row has no dollar-per-customer figure. Unrecorded cost, too few customers to divide by, too young to measure, or an event type this figure does not apply to. A blank is never a zero.",

    "new to tta":
        "Sign-ups with no TTA purchase before the event, including people who have never bought anything. A New sign-up who buys is a customer the event created.",

    "active":
        "Sign-ups who had bought within the 90 days before the event. They would mostly have shopped anyway; their spend is shown but not credited to the event.",

    "lapsed":
        "Sign-ups who had bought before, but not in the 90 days before the event. Bringing one back is worth something, but less than a brand-new customer.",

    "judged on":
        "Which bucket the event is for. Acquisition events are judged on New to TTA. Loyalty events exist for existing customers and are judged on Active — cost per net-new would answer the wrong question.",

    "target bought by +90d":
        "Sign-ups in the event's target bucket (New, or Active for loyalty events) who bought within 90 days of the event date.",

    "% of target signups":
        "Target-bucket sign-ups who bought within 90 days, as a share of target-bucket sign-ups. The conversion rate, with its denominator stated.",

    "$ / target customer (signups, 90d)":
        "Net cost divided by target-bucket sign-ups who bought within 90 days. The figure to compare events on. Blank until the event is 90 days old and has at least five such buyers.",

    "bought day-of":
        "Sign-ups in this bucket who bought on the event date.",

    "bought by +30d":
        "Sign-ups in this bucket who bought on the event date or within 30 days after. Includes the day-of buyers.",

    "bought by +90d":
        "Sign-ups in this bucket who bought on the event date or within 90 days after. Includes the +30d buyers.",

    "revenue by +90d":
        "Net sales from this bucket's sign-ups on the event date through 90 days after. For Active sign-ups, most of this would have happened anyway.",

    "% of signups (+90d)":
        "Sign-ups who bought within 90 days, as a share of all sign-ups in the bucket.",

    "resolvable to pos":
        "Sign-ups who have ever had a TTA point-of-sale record. For New sign-ups, this is the people who bought on or after the event; the rest have still never transacted — a result about the event, not a gap in the data.",

    "attendance":
        "Not shown anywhere on this tab. The sign-up list is who registered; check-in capture is not reliable enough to say who came. A showed / did-not-show split arrives when it is.",
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

    "events":
        "Did an event make people shop more than they would have anyway? Every number here is a comparison against what that day should have looked like — never a raw sales figure, because sales rise on Fridays and in December whether or not there was an event.\n\nSingle-store events are the strongest evidence: the other three stores act as a control group, so weather, holidays and anything citywide cancels out. Off-site events have no control group; treat their figures as a ceiling. The measure that holds up is **new customers** — first-time buyers rise on event days and the day after, and not before. Same-day sales lift does not clear the noise, so this tab does not make that claim.",

    "event_return":
        "Of the people on an event's sign-up list who bought anything, how many came back within 90 days — and does that differ for people meeting TTA for the first time versus existing customers who happened to attend?\n\nRead the funnel first. Most sign-ups never buy at all; the return rate is computed only on those who did, so on its own it overstates what an event achieves. A dash means too few people to make a rate.",

    "events_cost":
        "What each event cost TTA, and what that bought in new customers. Cost is marketing's corrected figure: the event's own spend, plus an even share of anything shared with other events (an agency retainer, a print run), minus what a brand partner covered. Airtable's own total is not used — it charges shared costs in full to every event they touch.\n\n**Unrecorded is not free.** Events with no budget line are listed but never divided into a per-customer figure. Every dollar-per-customer column says in its header what it is dividing by, because the answer changes a lot depending on the denominator. Quote the pooled series figures; single events swing on luck.",

    "event_tracker":
        "For each event: who signed up, split by whether they were new to TTA, active, or lapsed — then how many of each bought on the day, within 30 days, and within 90 days, and what it cost per customer the event actually created.\n\n**Signups, not attendance.** The list is who registered. Check-in capture is not reliable yet (one event: 160+ RSVPs, 23 checked in), so nothing here is per attendee and there is no showed / did-not-show split. That arrives when the capture side is fixed. Acquisition events are judged on the New bucket; loyalty events on the Active bucket, because they exist for existing customers.",
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
