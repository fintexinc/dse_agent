# -*- coding: utf-8 -*-
"""Console metrics reference — with a screenshot of every real component."""
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)
from PIL import Image as PILImage
import os

INK  = colors.HexColor("#1a1a1a"); DIM = colors.HexColor("#6b6b6b")
RULE = colors.HexColor("#d8d5cf"); BG  = colors.HexColor("#f2f0ec")
OK   = colors.HexColor("#2f6b46"); WARN= colors.HexColor("#8a6d1f")
BAD  = colors.HexColor("#a33a2a")
W    = 166*mm

def S(n, **kw):
    b = dict(fontName="Helvetica", fontSize=9.2, leading=13.4, textColor=INK,
             alignment=TA_LEFT, spaceAfter=5); b.update(kw); return ParagraphStyle(n, **b)

BODY=S("b"); SMALL=S("s", fontSize=8.2, leading=11.6, textColor=DIM)
H1=S("h1", fontName="Helvetica-Bold", fontSize=19, leading=23, spaceAfter=3)
SUB=S("sub", fontSize=9.6, leading=13, textColor=DIM, spaceAfter=13)
H2=S("h2", fontName="Helvetica-Bold", fontSize=13.5, leading=17, spaceBefore=15, spaceAfter=7)
H3=S("h3", fontName="Helvetica-Bold", fontSize=10.6, leading=14, spaceBefore=12, spaceAfter=4)
CAP=S("cap", fontSize=7.6, leading=10.4, textColor=DIM, spaceAfter=8)
MONO=S("m", fontName="Courier", fontSize=8.0, leading=11, textColor=colors.HexColor("#333"))
CELL=S("c", fontSize=8.3, leading=11.4); CELLB=S("cb", fontSize=8.3, leading=11.4, fontName="Helvetica-Bold")

story=[]
def h1(t,s=None):
    story.append(Paragraph(t,H1))
    if s: story.append(Paragraph(s,SUB))
def h2(t): story.append(Paragraph(t,H2))
def h3(t): story.append(Paragraph(t,H3))
def p(t,st=BODY): story.append(Paragraph(t,st))
def sp(h=6): story.append(Spacer(1,h))
def code(t):
    story.append(Table([[Paragraph(t.replace("\n","<br/>"),MONO)]],colWidths=[W],
        style=TableStyle([("BACKGROUND",(0,0),(-1,-1),BG),("BOX",(0,0),(-1,-1),0.4,RULE),
            ("LEFTPADDING",(0,0),(-1,-1),7),("RIGHTPADDING",(0,0),(-1,-1),7),
            ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)])))
    story.append(Spacer(1,7))

def shot(name, caption, max_w=W, max_h=118*mm):
    """The real component, screenshotted from the running console."""
    f = f"/tmp/shots/{name}.png"
    if not os.path.exists(f): return
    iw, ih = PILImage.open(f).size
    w = min(max_w, iw*0.19*mm); h = w*ih/iw
    if h > max_h: h = max_h; w = h*iw/ih
    img = Image(f, width=w, height=h)
    box = Table([[img]], colWidths=[w], style=TableStyle([
        ("BOX",(0,0),(-1,-1),0.5,RULE),("LEFTPADDING",(0,0),(-1,-1),0),
        ("RIGHTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),0),
        ("BOTTOMPADDING",(0,0),(-1,-1),0)]))
    story.append(KeepTogether([box, Spacer(1,3), Paragraph(caption, CAP)]))

def badge(k):
    m={"ok":("WORKS",OK),"broken":("BROKEN",BAD),"blocked":("NO DATA",WARN),"mis":("MISLEADING",WARN)}
    t,c=m[k]; return Paragraph(f'<font color="{c.hexval()}"><b>{t}</b></font>',CELL)

def table(head, rows, widths, aligns=None):
    data=[[Paragraph(h,CELLB) for h in head]]
    for r in rows:
        data.append([c if isinstance(c,Paragraph) else Paragraph(str(c),CELL) for c in r])
    st=[("BACKGROUND",(0,0),(-1,0),BG),("LINEBELOW",(0,0),(-1,0),0.5,RULE),
        ("LINEBELOW",(0,1),(-1,-2),0.25,colors.HexColor("#eceae6")),
        ("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),6),
        ("RIGHTPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),5),
        ("BOTTOMPADDING",(0,0),(-1,-1),5)]
    for i,a in enumerate(aligns or []):
        if a: st.append(("ALIGN",(i,0),(i,-1),a))
    story.append(Table(data,colWidths=widths,style=TableStyle(st),repeatRows=1))
    story.append(Spacer(1,9))

# =====================================================================
h1("DSE Control Plane — Metrics Reference",
   "Every number on Admin Home and Analytics: what it means, where it is computed, and whether it works today.<br/>"
   "Each component below is a screenshot of the <b>real console</b>, rendered from "
   "<font face='Courier'>control-plane/admin-ui</font> against the live production data on 2026-09-01.")

h2("1. Where the numbers come from")
p("Every figure on both screens is derived from three tables. Nothing is computed in the browser, and nothing is "
  "estimated unless this document says so.")
code("DSE Postgres                          Console (SQLite)        UI\n"
     "  audit_log ─────┐\n"
     "                 ├─► console_rm.runs_view ──────► runs ──────┐\n"
     "  model_call_ledger ─┘                                       ├─► analytics.ts ─► /analytics/home\n"
     "  work_items ──► console_rm.work_items_view ──► work_items ──┤                   /analytics\n"
     "  audit_log ───► console_rm.timeline_events ─► events ───────┘")
p("<b>A “run” is one agent turn</b> — not one work item, not one HTTP call. Each row in "
  "<font face='Courier'>runs_view</font> carries the stage that produced it (planner, coder, tester, reviewer), its "
  "token counts and its cost in USD.")
table(["Source","Live content (2026-09-01)"],
      [["runs_view","1,007 rows · $860.28 · 12,048,429 tokens · 2026-07-24 → 2026-09-01"],
       ["by stage","coder 502 ($851.61) · tester 267 ($4.51) · planner 204 ($4.02) · reviewer 34 ($0.14)"],
       ["work_items_view","184 rows — blocked 100 · failed 77 · pr_ready 7"],
       ["console_rm.baselines","0 rows — not configured"],
       ["status = done","never occurred (0 events)"]],
      [33*mm,133*mm])
p("Two of those lines govern most of what follows. <b>No work item has ever reached "
  "<font face='Courier'>done</font></b>, and <b>no ROI baseline is configured</b>. Between them they explain every "
  "“—” on the dashboard.", SMALL)

# ---------------------------------------------------------------- HOME
h2("2. Admin Home — Cost &amp; Impact")

h3("2.1 LLM spend, last 30 days")
shot("01-cost","The real card, live data.")
p("<b>計算</b>".replace("計算","Computation") + ": sum of <font face='Courier'>cost_usd</font> over every run whose "
  "<font face='Courier'>started_at</font> falls in the last 30 days. The sparkline buckets the same runs into 8 "
  "equal slices of that window. <font face='Courier'>analytics.ts:109</font>")
p("<b>Delta “+1627% mo”</b>: percentage change against the <i>previous</i> 30 days, with the tone inverted so that "
  "rising cost reads red. <font face='Courier'>analytics.ts:37</font>")
p("<b>Status: works, but the delta is not usable.</b> The figure is arithmetically correct and practically "
  "meaningless — the previous 30-day window contained almost no spend, so any activity at all produces a "
  "four-digit percentage. Consider suppressing the delta when the base period is below a threshold.", SMALL)

h3("2.2 Tokens, last 7 days")
shot("02-tokens","The real card, live data.")
p("<b>Computation</b>: sum of <font face='Courier'>tokens_in + tokens_out</font> over runs started in the last 7 "
  "days, compared with the 7 days before it. The bars are the same runs in 7 daily buckets.")
p("<b>Status: works.</b> The empty bars in the middle are real — they are days with no runs.", SMALL)

h3("2.3 Savings vs human estimate")
shot("03-savings","The real card. Both rows are empty because no baseline is configured.")
p("<b>Computation</b>: <font face='Courier'>completed_tasks × baseline_hours × hourly_rate − agent_cost</font>. "
  "It needs a <font face='Courier'>baselines</font> record (hourly rate and expected human hours per task), which "
  "has never been set.")
p("<b>Agent cost ($860.28)</b> is the sum of <font face='Courier'>cost_usd</font> over <b>all runs ever</b> — "
  "not the 30-day window used by the card at the top of the same row ($813.20). "
  "<font face='Courier'>analytics.ts:125</font>")
p("<b>Status: correctly blank, but the two cost figures mislead.</b> Rendering “—” without a baseline is the right "
  "behaviour — the alternative is inventing a human cost. The defect is that two numbers on one screen look like "
  "they should agree and never will, because one is all-time and the other is 30 days, and neither says so.", SMALL)

h2("3. Admin Home — Efficiency &amp; Quality")

h3("3.1 Development efficiency")
shot("04-efficiency","The real card. Five of its seven figures cannot populate today.")
table(["Figure","How it is computed","Status"],
      [["Headline (avg h)","Mean of <font face='Courier'>done_at − created_at</font> across completed items. "
        "<font face='Courier'>done_at</font> is the timeline event whose message starts with “→ done”.",badge("blocked")],
       ["Tasks completed","Count of items with status <font face='Courier'>done</font>.",badge("blocked")],
       ["In flight","Count of {queued, running, pr_ready, review_feedback}. The 7 shown are the "
        "<font face='Courier'>pr_ready</font> items.",badge("ok")],
       ["Avg h / task","Same mean as the headline.",badge("blocked")],
       ["Failed","Count of items with status <font face='Courier'>failed</font>.",badge("ok")],
       ["hours / task by category","Per repository: mean agent hours against the configured human baseline.",badge("blocked")]],
      [30*mm,109*mm,27*mm])
p("<b>These are gated on a status the product cannot currently reach.</b> An item becomes "
  "<font face='Courier'>done</font> only after a human merges its pull request. The DSE has opened 14 pull requests "
  "in its history and none were merged, so this card has never had an input. Not a calculation bug — a definition "
  "question, addressed in §6.", SMALL)

h3("3.2 Code quality / accuracy")
shot("05-quality","The real card. Note the legend: “Failed 77%”.")
table(["Figure","How it is computed","Status"],
      [["First-pass rate","<font face='Courier'>(completed − reworked) / completed</font>, where reworked means the "
        "timeline contains a review-feedback event.",badge("blocked")],
       ["Merged","Count of status <font face='Courier'>done</font> — a proxy. No GitHub merge state is read.",badge("mis")],
       ["Changes requested","Completed items whose timeline shows review rework.",badge("blocked")],
       ["Failed runs","<b>Counts work items, not runs.</b> It is the same 77 shown as “Failed” on the efficiency "
        "card. <font face='Courier'>analytics.ts:205</font>",badge("broken")],
       ["PRs open","Count of {pr_ready, review_feedback}.",badge("ok")],
       ["Donut legend","<b>Renders a count with a percent sign.</b> “Failed 77%” is 77 work items; the real share "
        "is 42%. <font face='Courier'>admin-ui/src/app/page.tsx:316</font>",badge("broken")]],
      [30*mm,109*mm,27*mm])

h2("4. Admin Home — Work item activity")
shot("06-overview","Plain counts by status — the most trustworthy numbers on the page.")
shot("07-bystatus","The same counts as a list. “Blocked” is the largest bucket on the dashboard.")
shot("08-bychannel","Work items grouped by the intake channel that created them.")
p("<b>Status: all three work.</b> They are direct counts over a table the projector maintains. Worth noting that "
  "<b>blocked (100) is the biggest bucket anywhere on the dashboard and nothing explains why</b> — there is no "
  "breakdown of blocking reason on either screen. That is a missing metric, not a broken one.", SMALL)

# ---------------------------------------------------------------- ANALYTICS
h2("5. Analytics")
p("The three tabs read the same two tables, restricted to the selected range and filters. The range applies to "
  "<font face='Courier'>started_at</font> for runs and to <font face='Courier'>created_at</font>/"
  "<font face='Courier'>updated_at</font> for work items, so a long-running item can appear in a window its runs "
  "do not.")

h3("5.1 Filters")
shot("10-filters","The real filter bar.")
table(["Filter","Behaviour","Status"],
      [["Date range","7d / 30d / 90d / QTD. QTD starts at the first day of the current calendar quarter.",badge("ok")],
       ["Repository","Filters work items by repo, then keeps only the runs belonging to those items.",badge("ok")],
       ["Agent","Filters by the runtime assigned to the item.",badge("ok")],
       ["Project · Task type","<b>Inert.</b> Both are hard-coded to a single option and filter nothing.",badge("broken")]],
      [30*mm,109*mm,27*mm])

h3("5.2 Cost tab")
shot("11-cost-summary","The four summary cards, live.")
table(["Figure","How it is computed","Status"],
      [["Total cost","Sum of <font face='Courier'>cost_usd</font> over runs in range.",badge("ok")],
       ["Tokens","Sum of <font face='Courier'>tokens_in + tokens_out</font> over the same runs.",badge("ok")],
       ["Runs","Row count of those runs — agent turns, not work items.",badge("ok")],
       ["Cost / completed task","<font face='Courier'>total_cost / completed_items</font>. “—” while nothing is "
        "completed, which is the current state.",badge("blocked")]],
      [30*mm,109*mm,27*mm])
shot("12-cost-trend","Cost over time — the runs in range, bucketed into 8 equal slices.")
p("<b>Status: works.</b> The shape is real: the peak at W3 is the day the fix loop ran hardest.", SMALL)
shot("13-cost-repo","Cost by repository.")
p("<b>Computation</b>: each run is attributed to its work item's repository, then summed. Runs whose item falls "
  "outside the filtered set land in “unknown”. <b>Status: works.</b>", SMALL)
shot("14-cost-model","Cost by model — a single bar, and the wrong one.")
p("<b>Status: broken.</b> The chart sums cost by the model name recorded on each run, and every stage is recorded "
  "as <font face='Courier'>anthropic/claude-haiku</font> — including the Coder, whose configured model is "
  "<font face='Courier'>anthropic/claude</font>. The cost per run proves they are not the same model:")
code("coder     467 runs   $822.21   =  $1.76 / run    labelled haiku\n"
     "planner   204 runs   $4.02     =  $0.02 / run    labelled haiku\n"
     "tester    267 runs   $4.51     =  $0.02 / run    labelled haiku")
p("A ninety-fold difference in cost per run between calls to the same model is not plausible. The label originates "
  "in <font face='Courier'>model_call_ledger</font> at metering time; the projector copies it verbatim. A further "
  "35 Coder runs ($29.40) carry no model at all. <b>This chart exists to support choosing a cheaper model, and it "
  "would actively mislead that decision.</b>", SMALL)

h3("5.3 Efficiency tab")
shot("20-efficiency-summary","The four summary cards. Three of them cannot populate.")
table(["Figure","How it is computed","Status"],
      [["Tasks completed","Count of status <font face='Courier'>done</font> in range.",badge("blocked")],
       ["Avg hours / task","Mean wall-clock from item creation to its “→ done” event.",badge("blocked")],
       ["Human baseline","Read from the configured baseline; “—” when unset.",badge("blocked")],
       ["Active runtimes","Distinct non-null runtimes across items in range.",badge("ok")]],
      [30*mm,109*mm,27*mm])
shot("21-efficiency-trend","Throughput — completed items per bucket. Empty because nothing completes.")
shot("22-efficiency-a","“Avg hours by repository”.")
p("<b>Status: broken.</b> The title promises an average; the code aggregates with a <b>sum</b> and never divides. "
  "<font face='Courier'>analytics.ts:321-323</font>. With at most one completed item per repository the two would "
  "agree, which is why it has never been noticed.", SMALL)
shot("23-efficiency-b","Work items by status — done / in progress / failed.")
p("<b>Status: works</b>, with the same count-as-percent legend defect as the Admin Home donut.", SMALL)

h3("5.4 Quality tab")
shot("30-quality-summary","The four summary cards.")
table(["Figure","How it is computed","Status"],
      [["First-pass rate","Completed items with no review-rework event, over completed items.",badge("blocked")],
       ["Changes requested","Completed items that were reworked.",badge("blocked")],
       ["Failed","Count of failed items in range. Correctly labelled here, unlike Admin Home.",badge("ok")],
       ["PRs open","Count of {pr_ready, review_feedback}.",badge("ok")]],
      [30*mm,109*mm,27*mm])
shot("31-quality-trend","Completions per bucket.")
shot("32-quality-a","Failures by repository — real counts.")
shot("33-quality-b","Outcomes donut, with the same percent defect.")

# ---------------------------------------------------------------- DEFECTS
h2("6. The structural question: nothing is ever “done”")
p("Eleven of the figures above report “no data”, and they share one cause. Every efficiency and quality metric on "
  "both screens is gated on work items reaching status <font face='Courier'>done</font>, and "
  "<font face='Courier'>done</font> is assigned only after a human merges the pull request. In the platform's "
  "entire history no item has ever reached it.")
p("<b>The metrics are not wrong. The definition of “completed” is one the operator cannot satisfy by using the "
  "product.</b> The result is a dashboard whose two headline quality panels have never displayed a value, while "
  "the platform has in fact produced 14 pull requests.")
table(["Option","What changes","Trade-off"],
      [["Leave as is","Nothing. The panels populate the first time somebody merges a DSE pull request.",
        "Honest, and useless until then. The dashboard cannot show progress on the work being done."],
       ["Add a second milestone","Track “delivered” (PR opened and green) alongside “done” (merged). Efficiency "
        "measures creation-to-PR; quality keeps measuring merges.",
        "Two definitions to explain, but each answers a real question: how fast the agent works, and how often its "
        "work is accepted."],
       ["Redefine done as PR-opened","One line in the status mapping.",
        "Cheapest, and it destroys the quality panel: “merged without rework” means nothing if nothing was merged. "
        "<b>Not recommended.</b>"]],
      [28*mm,68*mm,70*mm])
p("<b>Recommendation: the second.</b> It is the only option that lets the dashboard describe what the platform "
  "actually does today — open pull requests, and not get them merged — instead of showing a blank card that reads "
  "as “no activity”.", SMALL)

h2("7. Summary and suggested order of work")
table(["#","Item","Action","Effort"],
      [["1","Donut legend renders counts as percentages (both screens)","Fix","1 line × 2 files"],
       ["2","Cost by model attributed to the wrong model","Fix or hide","Metering change"],
       ["3","“Failed runs” label counts work items","Rename","1 line"],
       ["4","Agent cost is all-time beside a 30-day figure","Label or rescope","1 line"],
       ["5","“Avg hours by repository” sums instead of averaging","Fix","1 line"],
       ["6","Project / Task type filters do nothing","Remove","Delete two controls"],
       ["7","“+1627%” delta from a near-zero base","Suppress below a floor","1 condition"],
       ["8","Nothing ever reaches “done”","Product decision — §6","Design first"],
       ["9","“Blocked” is the biggest bucket and unexplained","Add a reason breakdown","New query"]],
      [8*mm,84*mm,38*mm,36*mm],aligns=["CENTER"])
p("Items 1, 3, 4, 5, 6 and 7 are single-line corrections to display logic and carry no risk. Item 2 changes what is "
  "recorded and should be verified against a known model call before it is trusted. Item 8 is not an engineering "
  "task — it is a decision about what the product counts as finished, and every empty panel above is waiting on "
  "it.", SMALL)

def deco(c,d):
    c.saveState(); c.setFont("Helvetica",7.4); c.setFillColor(DIM)
    c.drawString(22*mm,12*mm,"Fintex DSE · Control Plane metrics reference · components captured from the live console, 2026-09-01")
    c.drawRightString(188*mm,12*mm,"%d"%d.page)
    c.setStrokeColor(RULE); c.setLineWidth(0.4); c.line(22*mm,15.5*mm,188*mm,15.5*mm); c.restoreState()

doc=BaseDocTemplate("/Users/saraiva/Documents/DSE/fase1/docs/DSE-Console-Metrics-Reference.pdf",
    pagesize=A4,leftMargin=22*mm,rightMargin=22*mm,topMargin=20*mm,bottomMargin=20*mm,
    title="DSE Control Plane — Metrics Reference",author="Fintex DSE")
doc.addPageTemplates([PageTemplate(id="p",
    frames=[Frame(doc.leftMargin,doc.bottomMargin,doc.width,doc.height,id="f")],onPage=deco)])
doc.build(story)
print("ok")
