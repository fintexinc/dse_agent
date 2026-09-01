# -*- coding: utf-8 -*-
"""Builds the console metrics reference PDF."""
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle, KeepTogether)

INK = colors.HexColor("#1a1a1a")
DIM = colors.HexColor("#6b6b6b")
RULE = colors.HexColor("#d8d5cf")
BG = colors.HexColor("#f2f0ec")
OK = colors.HexColor("#2f6b४6".replace("४","4"))
OK = colors.HexColor("#2f6b46")
WARN = colors.HexColor("#8a6d1f")
BAD = colors.HexColor("#a33a2a")

ss = getSampleStyleSheet()
def S(name, **kw):
    base = dict(fontName="Helvetica", fontSize=9.2, leading=13.4, textColor=INK,
                alignment=TA_LEFT, spaceAfter=5)
    base.update(kw); return ParagraphStyle(name, **base)

BODY   = S("body")
SMALL  = S("small", fontSize=8.2, leading=11.6, textColor=DIM)
H1     = S("h1", fontName="Helvetica-Bold", fontSize=19, leading=23, spaceAfter=3, spaceBefore=0)
SUB    = S("sub", fontSize=9.6, leading=13, textColor=DIM, spaceAfter=14)
H2     = S("h2", fontName="Helvetica-Bold", fontSize=13.5, leading=17, spaceBefore=16, spaceAfter=7)
H3     = S("h3", fontName="Helvetica-Bold", fontSize=10.4, leading=14, spaceBefore=11, spaceAfter=4)
MONO   = S("mono", fontName="Courier", fontSize=8.0, leading=11, textColor=colors.HexColor("#333"))
CELL   = S("cell", fontSize=8.3, leading=11.4)
CELLB  = S("cellb", fontSize=8.3, leading=11.4, fontName="Helvetica-Bold")
CELLD  = S("celld", fontSize=8.3, leading=11.4, textColor=DIM)

story = []
def h1(t, s=None):
    story.append(Paragraph(t, H1))
    if s: story.append(Paragraph(s, SUB))
def h2(t): story.append(Paragraph(t, H2))
def h3(t): story.append(Paragraph(t, H3))
def p(t, st=BODY): story.append(Paragraph(t, st))
def code(t):
    story.append(Table([[Paragraph(t.replace("\n","<br/>"), MONO)]], colWidths=[165*mm],
        style=TableStyle([("BACKGROUND",(0,0),(-1,-1),BG),("BOX",(0,0),(-1,-1),0.4,RULE),
                          ("LEFTPADDING",(0,0),(-1,-1),7),("RIGHTPADDING",(0,0),(-1,-1),7),
                          ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)])))
    story.append(Spacer(1, 6))
def sp(h=5): story.append(Spacer(1, h))

def table(head, rows, widths, aligns=None):
    data = [[Paragraph(h, CELLB) for h in head]]
    for r in rows:
        data.append([c if isinstance(c, Paragraph) else Paragraph(str(c), CELL) for c in r])
    st = [("BACKGROUND",(0,0),(-1,0),BG),
          ("LINEBELOW",(0,0),(-1,0),0.5,RULE),
          ("LINEBELOW",(0,1),(-1,-2),0.25,colors.HexColor("#eceae6")),
          ("VALIGN",(0,0),(-1,-1),"TOP"),
          ("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
          ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]
    for i, a in enumerate(aligns or []):
        if a: st.append(("ALIGN",(i,0),(i,-1),a))
    story.append(Table(data, colWidths=widths, style=TableStyle(st), repeatRows=1))
    story.append(Spacer(1, 8))

def badge(kind):
    m = {"ok": ("WORKS", OK), "broken": ("BROKEN", BAD), "blocked": ("NO DATA", WARN),
         "mislabel": ("MISLEADING", WARN)}
    t, c = m[kind]
    return Paragraph(f'<font color="{c.hexval()}"><b>{t}</b></font>', CELL)

# ---------------------------------------------------------------- content
h1("DSE Control Plane — Metrics Reference",
   "What every number on Admin Home and Analytics means, where it is computed, and whether it currently works.<br/>"
   "Verified against the live control plane on 2026-09-01. Source of truth: <font face='Courier'>control-plane/api/src/analytics.ts</font> "
   "and the <font face='Courier'>console_rm</font> read model.")

h2("1. Where the numbers come from")
p("Every figure on both screens is derived from three tables. Nothing on these screens is computed in the browser, "
  "and nothing is estimated unless this document says so explicitly.")
code("DSE Postgres                     Console (SQLite)          UI\n"
     "  audit_log ─┐\n"
     "             ├─► console_rm.runs_view ──────► runs ────────┐\n"
     "  model_call_ledger ─┘                                     ├─► analytics.ts ─► /api/metrics\n"
     "  work_items ───► console_rm.work_items_view ─► work_items ┤                   /api/analytics\n"
     "  audit_log ───► console_rm.timeline_events ─► events ─────┘")
p("<b>A “run” is one agent turn</b>, not one work item and not one HTTP call. Each row in "
  "<font face='Courier'>runs_view</font> carries the stage that produced it (planner, coder, tester, reviewer), "
  "its token counts and its cost in USD. The projector writes one row per metered model call.")

h3("Live shape of the data (2026-09-01)")
table(["Source", "Content"],
      [["runs_view", "1,007 rows · $860.28 · 12,048,429 tokens · 2026-07-24 → 2026-09-01"],
       ["by stage", "coder 502 ($851.61) · tester 267 ($4.51) · planner 204 ($4.02) · reviewer 34 ($0.14)"],
       ["work_items_view", "184 rows — blocked 100 · failed 77 · pr_ready 7"],
       ["console_rm.baselines", "0 rows — not configured"],
       ["status = done", "never occurred (0 events)"]],
      [32*mm, 133*mm])

p("Two of those lines govern most of what follows. <b>No work item has ever reached <font face='Courier'>done</font></b>, "
  "and <b>no ROI baseline is configured</b>. Between them they explain every “—” on the dashboard.", SMALL)

# ---------------------------------------------------------------- home
h2("2. Admin Home")

h3("2.1 Cost &amp; Impact")
table(["Metric", "How it is computed", "Status"],
      [["LLM spend<br/>last 30 days",
        "Sum of <font face='Courier'>cost_usd</font> over every run started in the last 30 days. The sparkline "
        "buckets the same runs into 8 equal slices of that window.<br/>"
        "<font face='Courier'>analytics.ts:109</font>", badge("ok")],
       ["Delta “+1627% mo”",
        "Percentage change against the <i>previous</i> 30 days. Tone is inverted — rising cost is red.<br/>"
        "<font face='Courier'>analytics.ts:37</font>", badge("mislabel")],
       ["Tokens<br/>last 7 days",
        "Sum of <font face='Courier'>tokens_in + tokens_out</font> over runs started in the last 7 days, "
        "against the 7 days before it.", badge("ok")],
       ["Agent cost",
        "Sum of <font face='Courier'>cost_usd</font> over <b>all runs ever</b> — not the 30-day window used by "
        "the card beside it.<br/><font face='Courier'>analytics.ts:125</font>", badge("mislabel")],
       ["Human estimate<br/>and savings",
        "<font face='Courier'>completed_tasks × baseline_hours × hourly_rate − agent_cost</font>. Requires the "
        "<font face='Courier'>baselines</font> key; renders “—” without it, by design.", badge("blocked")]],
      [30*mm, 108*mm, 27*mm])
p("The 1627% delta is arithmetically correct and practically meaningless: the previous 30-day window contained "
  "almost no spend, so any activity produces a four-digit percentage. <b>Agent cost ($860.28) and LLM spend "
  "($813.20) differ only because they cover different periods</b> — all-time versus 30 days — and the UI does not "
  "say so. Two numbers that look like they should match, and never will.", SMALL)

h3("2.2 Development efficiency")
table(["Metric", "How it is computed", "Status"],
      [["Headline (avg hours)",
        "Mean of <font face='Courier'>done_at − created_at</font> across completed work items. "
        "<font face='Courier'>done_at</font> comes from the timeline event whose message starts with “→ done”.", badge("blocked")],
       ["Tasks completed", "Count of work items with status <font face='Courier'>done</font>.", badge("blocked")],
       ["In flight", "Count of status in {queued, running, pr_ready, review_feedback}. Currently 7 = the "
        "<font face='Courier'>pr_ready</font> items.", badge("ok")],
       ["Avg h / task", "Same mean as the headline.", badge("blocked")],
       ["Failed", "Count of work items with status <font face='Courier'>failed</font>. Currently 77.", badge("ok")],
       ["hours / task by category",
        "Per repository: mean agent hours for completed items, against the configured human baseline.", badge("blocked")],
       ["“x% vs baseline”",
        "<font face='Courier'>1 − avg_agent_hours / baseline_hours</font>. Needs both a completion and a baseline.", badge("blocked")]],
      [30*mm, 108*mm, 27*mm])
p("<b>Five of the seven figures on this card cannot ever populate today.</b> They are all gated on "
  "<font face='Courier'>status = done</font>, and an item only reaches <font face='Courier'>done</font> after a "
  "human merges its pull request. The DSE has produced 14 pull requests in its history and none were merged, so the "
  "card has never had an input. This is not a calculation bug — it is a definition question, addressed in §5.", SMALL)

h3("2.3 Code quality / accuracy")
table(["Metric", "How it is computed", "Status"],
      [["First-pass rate",
        "<font face='Courier'>(completed − reworked) / completed</font>, where “reworked” means the item's timeline "
        "contains a review-feedback event.", badge("blocked")],
       ["Merged", "Count of status <font face='Courier'>done</font> — <b>a proxy for merged, not a merge check</b>. "
        "No GitHub merge state is read.", badge("mislabel")],
       ["Changes requested", "Completed items whose timeline shows review rework.", badge("blocked")],
       ["Failed runs", "<b>Mislabelled.</b> This is the count of failed <i>work items</i> (77), not failed runs. "
        "It is the same number shown as “Failed” on the efficiency card.", badge("broken")],
       ["PRs open", "Count of status in {pr_ready, review_feedback}. Currently 7.", badge("ok")],
       ["Donut legend<br/>(0% / 0% / 77%)",
        "<b>Broken.</b> The legend appends “%” to a raw count. “Failed 77%” is 77 work items, not 77 per cent.<br/>"
        "<font face='Courier'>admin-ui/src/app/page.tsx:316</font>", badge("broken")]],
      [30*mm, 108*mm, 27*mm])

h3("2.4 Work item activity")
table(["Metric", "How it is computed", "Status"],
      [["Active / Completed (7d) / Blocked / Failed",
        "Direct counts over <font face='Courier'>work_items</font> by status.", badge("ok")],
       ["By status", "Same counts, rendered as a list.", badge("ok")],
       ["By channel", "Count of work items grouped by the intake channel that created them.", badge("ok")]],
      [30*mm, 108*mm, 27*mm])
p("These are the most trustworthy numbers on the page — plain counts over a table the projector maintains. "
  "Worth noting that <b>blocked (100) is the largest bucket on the whole dashboard and nothing anywhere explains "
  "why</b>: there is no breakdown of blocking reason on either screen.", SMALL)

# ---------------------------------------------------------------- analytics
h2("3. Analytics")
p("The three tabs read the same two tables as Admin Home, restricted to the selected date range and filters. "
  "The range applies to <font face='Courier'>started_at</font> for runs and to "
  "<font face='Courier'>created_at</font>/<font face='Courier'>updated_at</font> for work items, so a long-running "
  "item can appear in a window its runs do not.")

h3("3.1 Filters")
table(["Filter", "Behaviour", "Status"],
      [["Date range", "7d / 30d / 90d / QTD. QTD starts at the first day of the current calendar quarter.", badge("ok")],
       ["Repository", "Filters work items by repo, then keeps only the runs belonging to those items.", badge("ok")],
       ["Agent", "Filters by the runtime assigned to the item.", badge("ok")],
       ["Project / Task type", "<b>Inert.</b> Both are hard-coded to a single option and filter nothing.", badge("broken")]],
      [30*mm, 108*mm, 27*mm])

h3("3.2 Cost tab")
table(["Metric", "How it is computed", "Status"],
      [["Total cost", "Sum of <font face='Courier'>cost_usd</font> over runs in range.", badge("ok")],
       ["Tokens", "Sum of <font face='Courier'>tokens_in + tokens_out</font> over the same runs.", badge("ok")],
       ["Runs", "Row count of those runs — i.e. agent turns, not work items.", badge("ok")],
       ["Cost / completed task",
        "<font face='Courier'>total_cost / completed_items</font>. Renders “—” while nothing is completed, "
        "which is the current state.", badge("blocked")],
       ["Cost over time", "The same runs bucketed into 8 equal slices of the range.", badge("ok")],
       ["Cost by repository",
        "Each run is attributed to its work item's repository, then summed. Runs whose item is outside the "
        "filtered set fall into “unknown”.", badge("ok")],
       ["Cost by model",
        "<b>Wrong.</b> Sums cost by the model name recorded on the run. Every stage is recorded as "
        "<font face='Courier'>anthropic/claude-haiku</font>, including the Coder, whose configured model is "
        "<font face='Courier'>anthropic/claude</font>. See §4.2.", badge("broken")],
       ["Recent runs table", "The last 6 runs in range, newest first.", badge("ok")]],
      [30*mm, 108*mm, 27*mm])

h3("3.3 Efficiency tab")
table(["Metric", "How it is computed", "Status"],
      [["Tasks completed", "Count of status <font face='Courier'>done</font> in range.", badge("blocked")],
       ["Avg hours / task", "Mean wall-clock from item creation to its “→ done” event.", badge("blocked")],
       ["Human baseline", "Read straight from the configured baseline. “—” when unset.", badge("blocked")],
       ["Active runtimes", "Distinct non-null runtimes assigned across items in range.", badge("ok")],
       ["Throughput", "Completed items per bucket.", badge("blocked")],
       ["Avg hours by repository",
        "<b>Mislabelled.</b> The title says “avg”, but the value is the <i>sum</i> of hours per repository — the "
        "aggregation used is a sum with no division.", badge("broken")],
       ["Work items by status", "Donut of done / in-progress / failed counts.", badge("ok")],
       ["Runtime table (Saved h)",
        "<font face='Courier'>(baseline_hours − avg_hours) × completed</font>, per runtime. An estimate, and "
        "empty without a baseline.", badge("blocked")]],
      [30*mm, 108*mm, 27*mm])

h3("3.4 Quality tab")
table(["Metric", "How it is computed", "Status"],
      [["First-pass rate", "As on Admin Home: completed items with no review-rework event.", badge("blocked")],
       ["Changes requested", "Completed items that were reworked.", badge("blocked")],
       ["Failed", "Count of failed work items in range. Correctly labelled here, unlike Admin Home.", badge("ok")],
       ["PRs open", "Count of {pr_ready, review_feedback}.", badge("ok")],
       ["Completions", "Completed items per bucket.", badge("blocked")],
       ["Failures by repository", "Failed item counts grouped by repository.", badge("ok")],
       ["Outcomes donut", "Same count-rendered-as-percent defect as Admin Home.", badge("broken")],
       ["Repository table — “Reworked” column",
        "<b>Wrong.</b> Reads <font face='Courier'>status == review_feedback</font> (a current state) instead of the "
        "rework history, and it is gated behind a global counter, so it shows 0 whenever nothing was reworked "
        "anywhere.", badge("broken")]],
      [30*mm, 108*mm, 27*mm])

# ---------------------------------------------------------------- defects
h2("4. Defects found, with the evidence")

h3("4.1 The donut legend renders counts as percentages")
p("<b>Where:</b> <font face='Courier'>admin-ui/src/app/page.tsx:316</font> and "
  "<font face='Courier'>admin-ui/src/app/analytics/page.tsx:247</font> — both render "
  "<font face='Courier'>{q.value}%</font>, where <font face='Courier'>q.value</font> is a raw count.")
p("<b>Effect:</b> the dashboard currently reads “Failed 77%”. The real figure is 77 work items out of 184, "
  "which is 42%. Every donut legend on both screens is affected.")
p("<b>Fix:</b> divide by the segment total before rendering, or drop the “%” and label the column “items”. "
  "One line each. <b>Recommendation: fix.</b>")

h3("4.2 Cost by model attributes the Coder's spend to the wrong model")
p("<b>Where:</b> <font face='Courier'>model_call_ledger</font> records the model at metering time; the projector "
  "copies it into <font face='Courier'>runs_view.model</font> verbatim.")
p("<b>Evidence:</b> the ledger labels every stage <font face='Courier'>anthropic/claude-haiku</font>, but the "
  "cost per run is not consistent with one model:")
code("coder     467 runs   $822.21   =  $1.76 / run    labelled haiku\n"
     "planner   204 runs   $4.02     =  $0.02 / run    labelled haiku\n"
     "tester    267 runs   $4.51     =  $0.02 / run    labelled haiku")
p("A ninety-fold difference in cost per run between calls to the same model is not plausible. The Coder's "
  "configured model is <font face='Courier'>anthropic/claude</font>; the recorded name is a default that does not "
  "reflect the call. A further 35 Coder runs ($29.40) carry no model at all.")
p("<b>Effect:</b> “Cost by model” shows a single bar for a model that is not the one doing the spending. The chart "
  "is unusable for its purpose — choosing a cheaper model — and would actively mislead that decision.")
p("<b>Fix:</b> record the model actually returned by the gateway response rather than the requested default. "
  "<b>Recommendation: fix, or hide the chart until the label is trustworthy.</b> A wrong attribution is worse "
  "than an absent one.")

h3("4.3 “Failed runs” on Admin Home counts work items")
p("<b>Where:</b> <font face='Courier'>analytics.ts:205</font> — the stat is built from the failed <i>work item</i> "
  "count while the label says runs. The same variable feeds “Failed” on the efficiency card, which is why both "
  "read 77.")
p("<b>Fix:</b> either rename the label to “Failed items”, or compute it from "
  "<font face='Courier'>runs.status = 'error'</font>, which the table already carries. "
  "<b>Recommendation: rename.</b> The run-level figure is available but nobody has asked for it.")

h3("4.4 Two cost figures on one screen, over different windows")
p("Card 01 shows a 30-day total; card 03 shows an all-time total. Both are labelled simply as cost. "
  "<b>Recommendation: fix</b> — put “all time” on the second, or scope it to the same window.")

h3("4.5 “Avg hours by repository” sums instead of averaging")
p("<b>Where:</b> <font face='Courier'>analytics.ts:321-323</font> aggregates with a sum and formats the result with "
  "an “h” suffix under a title that promises an average. With one completed item per repository the two agree, "
  "which is why it has never been noticed. <b>Recommendation: fix.</b>")

h3("4.6 Project and Task type filters do nothing")
p("Both are populated with a single hard-coded option and are never applied to the query. "
  "<b>Recommendation: remove</b> until there is a project or task-type dimension in the data. A control that "
  "cannot change the result teaches the operator to distrust the controls that can.")

# ---------------------------------------------------------------- the big one
h2("5. The structural question: nothing is ever “done”")
p("Eleven of the metrics above report “no data”, and they share one cause. Every efficiency and quality figure on "
  "both screens is gated on work items reaching status <font face='Courier'>done</font>, and "
  "<font face='Courier'>done</font> is only assigned after a human merges the pull request. In the platform's "
  "entire history no item has ever reached it.")
p("The result is a dashboard whose two headline quality panels have never displayed a value, while the platform "
  "has in fact produced 14 pull requests. <b>The metrics are not wrong; the definition of “completed” is one the "
  "operator cannot satisfy by using the product.</b>")
h3("Three options, with what each costs")
table(["Option", "What changes", "Trade-off"],
      [["Leave as is",
        "Nothing. The panels populate the first time somebody merges a DSE pull request.",
        "Honest, and useless until then. The dashboard cannot show progress on the thing being worked on."],
       ["Add a second milestone",
        "Track “delivered” (pull request opened and green) alongside “done” (merged). Efficiency measures "
        "creation-to-PR; quality keeps measuring merges.",
        "Two definitions to explain, but each answers a real question: how fast the agent works, and how often "
        "its work is accepted."],
       ["Redefine done as PR-opened",
        "One-line change to the status mapping.",
        "Cheapest, and it destroys the quality panel: “merged without rework” stops meaning anything if nothing "
        "was merged. <b>Not recommended.</b>"]],
      [28*mm, 68*mm, 69*mm])
p("<b>Recommendation: the second option.</b> It is the only one that lets the dashboard describe the platform's "
  "actual behaviour — which today is that it opens pull requests and does not get them merged — instead of "
  "showing a blank card that reads as “no activity”.", SMALL)

# ---------------------------------------------------------------- summary
h2("6. Summary and suggested order of work")
table(["#", "Item", "Action", "Effort"],
      [["1", "Donut legend renders counts as percentages", "Fix", "1 line × 2 files"],
       ["2", "Cost by model attributed to the wrong model", "Fix or hide", "Metering change"],
       ["3", "“Failed runs” label counts work items", "Rename", "1 line"],
       ["4", "Agent cost is all-time beside a 30-day figure", "Label or rescope", "1 line"],
       ["5", "“Avg hours by repository” sums instead of averaging", "Fix", "1 line"],
       ["6", "Project / Task type filters do nothing", "Remove", "Delete two controls"],
       ["7", "Nothing ever reaches “done”", "Product decision — §5", "Design first"],
       ["8", "“Blocked” is the biggest bucket and unexplained", "Add a reason breakdown", "New query"]],
      [8*mm, 82*mm, 40*mm, 35*mm], aligns=["CENTER"])
p("Items 1 and 3 through 6 are single-line corrections to display logic and carry no risk. Item 2 changes what is "
  "recorded and should be verified against a known model call before it is trusted. Item 7 is not an engineering "
  "task — it is a decision about what the product counts as finished, and every empty panel on the dashboard is "
  "waiting on it.", SMALL)

# ---------------------------------------------------------------- render
def deco(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.4)
    canvas.setFillColor(DIM)
    canvas.drawString(22*mm, 12*mm, "Fintex DSE · Control Plane metrics reference · verified 2026-09-01")
    canvas.drawRightString(188*mm, 12*mm, "%d" % doc.page)
    canvas.setStrokeColor(RULE); canvas.setLineWidth(0.4)
    canvas.line(22*mm, 15.5*mm, 188*mm, 15.5*mm)
    canvas.restoreState()

doc = BaseDocTemplate("/Users/saraiva/Documents/DSE/fase1/docs/DSE-Console-Metrics-Reference.pdf",
                      pagesize=A4, leftMargin=22*mm, rightMargin=22*mm,
                      topMargin=20*mm, bottomMargin=20*mm,
                      title="DSE Control Plane — Metrics Reference",
                      author="Fintex DSE")
frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")
doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=deco)])
doc.build(story)
print("ok")
