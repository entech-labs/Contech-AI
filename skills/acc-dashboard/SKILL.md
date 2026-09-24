---
name: acc-project-dashboard
description: Build an interactive project overview dashboard from live Autodesk Construction Cloud (ACC) data using the autodesk-connector tools. Use when the user asks for a project dashboard, project overview, status report, "how is the project doing", or to refresh an existing dashboard.
---

# ACC project overview dashboard

Build a one-page, interactive dashboard of an ACC project from **live** data pulled through
the `autodesk-connector` tools. Every time — including "refresh" — fetch fresh data; never
reuse numbers from earlier in the conversation.

## 1. Pull the data (in parallel where possible)

Use the project_id from the Claude Project instructions (or the one the user names).

| Call | Used for |
|---|---|
| `list_submittals` | Submittal KPIs, status breakdown, spec sections, overdue list |
| `list_rfis` | Open/closed RFIs, overdue RFIs, RFIs by assignee |
| `list_issues` | Open issues, issues by assignee, overdue issues |
| `list_project_users` | Turn user IDs into names — never show raw IDs |
| `list_budgets` (optional) | Budget vs. actual, only if the call succeeds |

If a call returns an error or 403, skip that section and show a small note
("RFIs unavailable — access not granted") instead of failing the whole dashboard.
Don't call `list_hubs` or `list_projects`.

## 2. Work out the numbers

- **Today** = the current date. An item is **overdue** when it has a due date before today and is
  not closed/answered/approved.
- Normalize status values to a few readable groups (e.g. Open, In review, Closed); keep the
  original value in tooltips or tables.
- Match assignee / manager IDs to names with `list_project_users`. Unknown IDs → "Unassigned".
- If there are more than 200 rows (the tool says so in `note`), call again with `save_csv=true`
  and base the numbers on the full set.

## 3. Layout (always the same, so people learn where to look)

1. **Header** — project name (or "the project" if the Claude Project says to hide names), and
   **"Data as of <date, time>"**.
2. **KPI row** (4 tiles) — Submittals total · Submittals overdue · Open RFIs · Open issues.
   Overdue tiles in an alert color, with the word "overdue" in the label too.
3. **Submittals by status** — bar chart.
4. **Submittals by spec section** — top 10 sections, horizontal bar chart.
5. **Open items by assignee** — RFIs + issues per person, stacked bars; this answers
   "who has the most?".
6. **Overdue list** — table of overdue submittals, RFIs and issues: type, title, assignee,
   due date, days overdue; sorted by days overdue, most first.
7. **Budget vs. actual** (only if budget data came back) — by line or trade.

## 4. Build it

- Produce a single self-contained HTML page (an artifact), readable on a projector: large
  numbers, clear labels, no more than 6 colors.
- Add simple filters where useful (e.g. by assignee, by status) that work in the browser.
- Keep it **read-only in spirit**: no buttons that suggest changing ACC data.
- End your reply with one line: what's on the dashboard and "ask me to refresh any time".

## 5. Refresh

When asked to refresh, repeat steps 1–4 with fresh calls and update the "Data as of" time.
Dashboards are snapshots; refreshing is how they stay current.
