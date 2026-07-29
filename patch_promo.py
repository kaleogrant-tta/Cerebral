import ast

p = r"C:\Users\User\cerebral\Cerebral\cerebral_public.py"
s = open(p, encoding="utf-8-sig").read()   # strips any BOM
hits = []

def swap(old, new, tag):
    global s
    if old in s:
        s = s.replace(old, new, 1)
        hits.append("OK  " + tag)
    else:
        hits.append("MISS " + tag)

# --- 1) tab order: Promo Lab second-to-last ---
swap("t_charts, t_insights, t_brands, t_redeem, t_projections, t_gloss, t_promo = st.tabs(",
     "t_charts, t_insights, t_brands, t_redeem, t_projections, t_promo, t_gloss = st.tabs(",
     "tab variables")
swap('["Charts", "Insights", "Brands", "Redemptions", "Projections", "What the terms mean", "Promo Lab"]',
     '["Charts", "Insights", "Brands", "Redemptions", "Projections", "Promo Lab", "What the terms mean"]',
     "tab labels")

# --- 2) blurbs ---
A = r'''    st.markdown('<p class="note"><b>How to read this.</b> This tab answers one question: where is it worth spending discount dollars? A customer counts as <b>churned</b> when they have not bought anything within the lapse window you set below. Every table is ranked by <b>Net gain</b> - the money a win-back promo is projected to make after paying for the discount itself - not by churn rate. That way small, noisy segments can never outrank big, reliable ones. Set the sliders to your own assumptions; nothing here is final until a real campaign measures a real response rate.</p>', unsafe_allow_html=True)'''
swap("    df = q(", A + "\n\n    df = q(", "top explainer")

B = r'''        st.markdown('<p class="note"><b>What you are looking at.</b> Each row is a product category across all stores. <b>Churn %</b> is the share of that category customers who have not come back within the lapse window. <b>Real margin</b> comes straight from your sales data, not a guess. <b>Targetable</b> is how many lapsed customers you could actually send an offer to. The greener the Net gain column, the more sense a discount makes there.</p>', unsafe_allow_html=True)'''
a2 = '        st.markdown("**Segments ranked by net gain from a win-back promo**")'
swap(a2, B + "\n" + a2, "churn map blurb")

C = r'''        st.markdown('<p class="note"><b>How to use this.</b> The first table picks the single best promo for each store - start there. The dropdown below it shows every category inside one store. The verdict table at the bottom tells you whether a store needs one targeted offer (churn concentrated in a few categories) or a store-wide event like a double-points week (churn spread across nearly everything).</p>', unsafe_allow_html=True)'''
a3 = '''        st.markdown("**Each store's single best promo (highest net gain)**")'''
swap(a3, C + "\n" + a3, "store blurb")

D = r'''        st.markdown('<p class="note"><b>What this means.</b> Brands ranked by the return on winning back their lapsed buyers. A <b>positive ROI</b> means the promo pays for itself under your assumptions. A negative one means the discount would cost more than it brings back - protect those brands and use them as traffic drivers in marketing instead of discounting them. High ROI with tiny dollar figures means interesting but not a priority.</p>', unsafe_allow_html=True)'''
a4 = '        st.markdown("**Brands ranked by win-back ROI**")'
swap(a4, D + "\n" + a4, "brand blurb")

print("\n".join(hits))
ast.parse(s)   # will raise if anything is broken
with open(p, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(s)
print("Syntax OK - file written")