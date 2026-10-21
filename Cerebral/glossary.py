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

    "regression":
        "A best-fit line through the weekly numbers. It picks the coefficients "
        "that make the week-to-week misses as small as possible, so each "
        "coefficient reads: holding the other drivers in the model fixed, one "
        "more of this is worth so many units a week. It finds patterns, not "
        "causes.",

    "coefficient":
        "The number a regression attaches to a driver: units per extra SKU in "
        "stock, units for a full promo week, and so on. Read it with its likely "
        "range (±2 standard errors). If that range crosses zero, the data cannot "
        "tell the effect from nothing yet.",

    "residual":
        "Actual minus what the model expected, for one week. The part of the "
        "week the model cannot explain - usually the interesting part. A big "
        "negative residual with a normal shelf is a demand dip; with a thin "
        "shelf it is a supply gap.",

    "r²":
        "R-squared: the share of the week-to-week ups and downs the model "
        "explains, from 0 to 1. Low is not wrong; it means most of the movement "
        "comes from things we do not measure.",

    "backtest":
        "The honest test. Refit the model on data up to eight weeks ago, "
        "forecast those eight weeks, and compare with what happened and with "
        "'same as last week'. A model that cannot beat that simple rival only "
        "describes the past.",

    "stock as a control":
        "Putting SKUs-in-stock into the regression so that the leftover is "
        "comparable week to week. It is not that the stock number is interesting "
        "in itself: it cleans the sales number, so a drop can be read as supply "
        "(the shelf was thin) or demand (the shelf was fine).",

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
        "The normal range for this category, worked out from the 13 weeks "
        "before the week being checked, using how much that category itself "
        "normally moves from week to week. A week only counts as outside it "
        "if it is more than three of those typical swings away AND at least "
        "two percentage points from the baseline. Most ordinary weeks stay "
        "inside; roughly 2 in 100 category-weeks have gone outside it over the "
        "past year. A point outside is worth a look — not proof anything is "
        "wrong, just a prompt to check.",

    "baseline":
        "The average of the last 13 weeks. The dotted line the shaded band is "
        "built around.",

    "run rule":
        "Ten weeks in a row on the same side of the baseline, or six weeks "
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
        "How often two categories land in the same basket, compared with how "
        "often their sizes alone would suggest.\n\n"
        "Only baskets already containing two or more categories are counted "
        "(over half of all baskets hold a single item). In those baskets even "
        "unrelated big categories score below 1.0, so 1.0 is NOT the neutral "
        "point. Pairs are read against the median pair instead: well below it "
        "(under 0.7x the median) is labelled substitutes, well above it (over "
        "1.3x) is labelled affinity. It is a relative read, not proof.",

    "substitutes":
        "A pair that shares baskets far less often than the typical pair. It "
        "may be two categories customers choose between, so demand could move "
        "rather than disappear if you cut one - worth checking before acting.",

    "affinity":
        "A pair that shares baskets far more often than the typical pair. "
        "Candidates for adjacent placement, bundles, or an upsell prompt.",

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
        "Sign-ups Alpine IQ has linked to a TTA point-of-sale record. Alpine builds that link from phone and email; a sign-up uploaded with an email the store profile does not carry never links, and their purchases are invisible here. Unresolved therefore means never bought OR bought under a key the roster lacks — the split is not known, so buyer counts are floors and dollars-per-customer are ceilings.",

    "attendance":
        "Not shown anywhere on this tab. The sign-up list is who registered; check-in capture is not reliable enough to say who came. A showed / did-not-show split arrives when it is.",

    "matchable":
        "Sign-ups with a phone number on the roster. Alpine IQ links a sign-up to a purchase on phone and email; email-only sign-ups link 0.5% of the time, phone-bearing ones 37%. So only matchable sign-ups can ever be seen buying, and every rate is computed on them. Email-only sign-ups are counted, never divided.",

    "% of matchable target signups":
        "Target-bucket sign-ups who bought within 90 days, as a share of target-bucket sign-ups that have a phone on the roster. The conversion rate with an honest denominator.",

    "$ / target customer (matchable signups, 90d)":
        "Net cost divided by matchable target-bucket sign-ups who bought within 90 days. The figure to compare events on. Still a ceiling: a phone on the roster is necessary for a match, not sufficient.",

    "$ / matchable signup":
        "Net cost divided by sign-ups that have a phone on the roster. A real number but a weak one: it rewards a long list.",

    "% of matchable (+90d)":
        "Sign-ups who bought within 90 days, as a share of matchable sign-ups in the bucket.",

    "% of matchable (day-of)":
        "Sign-ups who bought on the event date, as a share of matchable sign-ups in the bucket.",

    "% of matchable (+30d)":
        "Sign-ups who bought within 30 days, as a share of matchable sign-ups in the bucket.",

    # --- promos (Promo Lab) -----------------------------------------------
    "qualifying":
        "A basket that looks like the offer — the right brand, enough units, or "
        "enough spent — whether or not the discount was actually rung. There are "
        "no promo codes in the sales data, so this is worked out from what was "
        "in the basket. It counts the people the offer reached.",

    "applied":
        "A qualifying basket where the evidence says the deal was actually "
        "given: either the prices show it (three gummies averaging $20 each on "
        "a 3-for-$60 offer) or the till discount on the basket covers at least "
        "half of what the offer would take off. It is an estimate — the sales "
        "export carries each product's average price for the load period, not "
        "the price on the receipt, and any other discount on the basket can "
        "look like the offer. Applied divided by qualifying is the share of "
        "eligible baskets that got the deal; well under 100% means the till is "
        "not ringing it every time, or people are buying the brand without "
        "asking for the offer.",

    "near miss":
        "A basket one step short of qualifying — two gummies on a 3-for offer, "
        "three eighths on a 4-for offer, or a few dollars under a spend bar "
        "with no add-on. These are the customers a word at the counter would "
        "have tipped over, so a high count is an opportunity, not a failure.",

    "est. discount":
        "What the offer gave away: the brand's normal unit price (the clean "
        "run-up days before the promo, with days under another offer on the "
        "same brand left out) minus the price actually paid, times the units, "
        "added up over applied baskets. Where the price does not show the deal, "
        "the basket's till discount is used instead, capped at what the offer "
        "implies. An estimate, not a figure from the till.",

    "break-even volume lift":
        "How much more of the brand must sell for the margin dollars to come "
        "back to where they were before the discount: depth ÷ (margin − depth). "
        "A 20% discount on a 50% margin needs +67% units just to stand still. "
        "If the discount depth is as big as the margin, no amount of volume "
        "rescues it.",
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
        "One point outside the band is worth a look. Ten consecutive weeks "
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

    "tldr":
        "The short version. Each line is written by the refresh from the Model Lab "
        "fits and a few checks on the other tables: a finding (something sold "
        "more or less than its stock, season and promo calendar predicted), a "
        "suggestion (a reorder, a promo worth repeating) or a question the "
        "numbers raised. Bad before warning before good; the chain first, then "
        "each selected store. Nothing here is a conclusion - it is where to look "
        "first this week.",

    "alerts":
        "Generated automatically by comparing this week against the last 13. "
        "These are prompts to investigate, not conclusions. Categories under "
        "1% of revenue are excluded — the numbers are too small to be "
        "meaningful.",

    "lift":
        "Which categories get bought together more or less than the typical pair. "
        "Useful for menu layout, bundles and deciding whether cutting a "
        "category would move demand elsewhere or simply lose it.",

    "inventory":
        "Whether the money tied up in each category matches what it earns. "
        "The most common finding is a long tail of products with many SKUs, "
        "high stock and little revenue.",

    "events":
        "Did an event make people shop more than they would have anyway? Every number here is a comparison against what that day should have looked like — never a raw sales figure, because sales rise on Fridays and in December whether or not there was an event.\n\nSingle-store events are the strongest evidence: the other three stores act as a control group, so weather, holidays and anything citywide cancels out. Off-site events have no control group; treat their figures as a ceiling. The cards at the top are the live figures; if a range includes zero, no effect has been detected on that measure. **New customers** is the measure to judge events by: first-time buyers rise on the day, and not before. This note quotes no numbers on purpose — the cards are the source of truth.",

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
