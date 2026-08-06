# Copyright (c) 2026, Flitz Interactive and contributors
# For license information, please see license.txt
"""
Read-only API for the "Team Dashboard" tab of /vnphrms/dashboard.

This module imports shared helpers and the `lazy`/`endpoint` decorators from
dashboard/api/my_dashboard.py rather than restating them.

TEAM DEFINITION
---------------
`Employee` is a nested-set tree (`is_tree: 1`, `nsm_parent_field: reports_to`, lft/rgt
maintained by Frappe). "Team" is therefore resolved from DATA, not from any frontend
role.

DECISION: the default scope is the **full reporting subtree** -- every Active Employee
strictly inside the manager's `lft`/`rgt` bounds, at any depth. Rationale: the mock's
"Team size = 12 members" exceeds the 6 rows its own members table renders, implying a
set wider than direct reports; and lft/rgt makes a subtree exactly as cheap as one
level. Pass `scope="direct"` for direct reports only. The mock does not actually
disambiguate this -- see GAP-2.

AUTHORIZATION
-------------
Discovery found the frontend gates this tab on a role literally named "Manager", which
ships with no Frappe/ERPNext/HRMS app, so today only System Manager reaches it. That
defect is NOT replicated. Server-side rule:

    authorized  <=>  the caller's Employee has at least one report
                     OR the caller holds "HR Manager" / "System Manager"

An unauthorized or team-less caller receives the defined EMPTY contract (never a 500,
never someone else's rows).

ROW SCOPING
-----------
There are still no `permission_query_conditions` hooks anywhere in the bench, so scope
is enforced explicitly in the query layer: every widget query filters
`employee IN (<resolved team set>)`. Nothing outside the reporting subtree is readable
through this module.

Reads use `frappe.get_list`, so Frappe's role + User Permission checks also apply.
CAUTION: a User Permission restricting the manager's own `Employee` will shrink or empty
their team. That is a site-configuration conflict, not a bug here -- it is detected and
reported in `meta.warnings`. See GAP-6.

RESPONSE CONTRACT
-----------------
Shapes are lifted field-for-field from the Team Dashboard mock in the compiled bundle
(component `tQ`, `displayName = "TeamDashboard"`, bytes 1808848-1817872); each mock is
quoted above its builder. Presentation-only keys (`icon`, `iconBg`, `iconColor`,
`cardBorder`, `dotColor`, `color`) are not emitted -- they are JS component refs and
Tailwind class strings owned by the UI.

Frappe wraps whitelisted returns under "message"; clients read `response.message`.
"""

import datetime

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate, strip_html

# Shared helpers -- single source of truth lives in my_dashboard.
from vnp_hr_solutions_dashboard.dashboard.api.my_dashboard import (
	WEEKEND_FROM_LIST,
	WEEKEND_FROM_WEEKDAY,
	WEEKEND_RECONCILED,
	WINDOW_FULL_MONTH,
	_can,
	_cached,
	_company_currency,
	_daterange_label,
	_fetch_holidays,
	_fmt_money,
	_month_window,
	_num,
	_ok,
	_resolve_employee,
	_weekend_info,
	_window_end,
	endpoint,
	lazy,
)

# NOTE ON REUSE: weekend reconciliation, the window policy and the `lazy`/`endpoint`
# decorators have exactly ONE definition -- in my_dashboard -- so the two tabs cannot
# diverge. If a third tab is added, promote those into dashboard/api/_attendance.py and
# import from all three rather than copying.

# Cache keys are namespaced under the same "vnp_dash:my:" root as My Dashboard so the
# existing cache_hooks invalidation glob clears both. Widget names are prefixed "team:".
WIDGET = "team:{}"

MANAGER_ROLES = ("HR Manager", "System Manager")

# Attendance.status -> calendar `type`. The team calendar uses only these 4 types
# (the mock emits present | wfh | leave | absent and stats has exactly those 4).
TEAM_TYPE_MAP = {
	"Present": "present",
	"Work From Home": "wfh",
	"On Leave": "leave",
	"Absent": "absent",
}

# Titles the mock pairs with each type.
TEAM_TITLE_MAP = {
	"present": "Present",
	"wfh": "Work From Home",
	"leave": "On Leave",
	"absent": "Absent",
}

# `detail` keywords the UI's sub-tab filter substring-matches (lowercased):
#   "Leaves"   -> "leave"
#   "Expenses" -> "expense" | "claim"
#   "WFH"      -> "wfh" | "off" | "regularization"
# The strings built in _build_pending_approvals MUST keep these keywords.
CATEGORY_LEAVE = "leave"
CATEGORY_EXPENSE = "expense"
CATEGORY_WFH = "wfh"
CATEGORY_COMP_OFF = "comp_off"
CATEGORY_REGULARIZATION = "regularization"
CATEGORIES = (
	CATEGORY_LEAVE,
	CATEGORY_EXPENSE,
	CATEGORY_WFH,
	CATEGORY_COMP_OFF,
	CATEGORY_REGULARIZATION,
)

# Sub-tab label -> categories, mirroring the bundle's substring filter.
SUBTAB_CATEGORIES = {
	"all": CATEGORIES,
	"leaves": (CATEGORY_LEAVE,),
	"expenses": (CATEGORY_EXPENSE,),
	"wfh": (CATEGORY_WFH, CATEGORY_COMP_OFF, CATEGORY_REGULARIZATION),
}

# Mock's separator, reproduced exactly: two spaces either side of the middot.
SEP = "  ·  "


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _initials(full_name):
	parts = [p for p in cstr(full_name).split() if p]
	if not parts:
		return "?"
	if len(parts) == 1:
		return parts[0][:2].upper()
	return (parts[0][0] + parts[-1][0]).upper()


def _first_name(full_name):
	parts = [p for p in cstr(full_name).split() if p]
	return parts[0] if parts else cstr(full_name)


def _lwp_leave_types():
	"""Leave Types flagged unpaid. One query, reused for every member."""
	if not _can("Leave Type"):
		return set()
	return set(frappe.get_list("Leave Type", filters={"is_lwp": 1}, pluck="name", limit_page_length=0))


# ---------------------------------------------------------------------------
# team resolution + authorization
# ---------------------------------------------------------------------------


def _fetch_team(manager, scope="subtree"):
	"""Resolve the manager's team in ONE query.

	subtree -> nested-set bounds (any depth, excludes the manager)
	direct  -> reports_to == manager

	Returns (rows, tree_total). `tree_total` is a permission-free COUNT used only to
	detect User Permission shrinkage -- it returns an integer, never row data.
	"""
	if not _can("Employee"):
		return None, 0

	if scope == "direct":
		filters = [["reports_to", "=", manager.name], ["status", "=", "Active"]]
	else:
		bounds = frappe.db.get_value("Employee", manager.name, ["lft", "rgt"], as_dict=True)
		if not bounds or bounds.lft is None or bounds.rgt is None:
			# Tree not built (nested set never rebuilt). Fall back to direct reports
			# rather than silently returning an empty team.
			filters = [["reports_to", "=", manager.name], ["status", "=", "Active"]]
		else:
			filters = [
				["lft", ">", bounds.lft],
				["rgt", "<", bounds.rgt],
				["status", "=", "Active"],
			]

	rows = frappe.get_list(
		"Employee",
		filters=filters,
		fields=[
			"name",
			"employee_name",
			"image",
			"holiday_list",
			"company",
			"designation",
			"date_of_joining",
		],
		order_by="employee_name asc",
		limit_page_length=0,
	)
	# COUNT only -- no rows cross this boundary.
	return rows, _tree_count(filters)


def _tree_count(filters):
	"""Permission-free COUNT for the same filter set (integer only)."""
	try:
		conds = []
		values = {}
		for i, (field, op, val) in enumerate(filters):
			key = f"v{i}"
			conds.append(f"`{field}` {op} %({key})s")
			values[key] = val
		return cint(
			frappe.db.sql(
				f"SELECT COUNT(*) FROM `tabEmployee` WHERE {' AND '.join(conds)}", values
			)[0][0]
		)
	except Exception:
		return 0


def _authorize(manager, team):
	"""(authorized, warnings). Never throws for a team-less caller -- returns the
	defined empty contract instead."""
	roles = set(frappe.get_roles())
	privileged = bool(roles & set(MANAGER_ROLES))

	if not manager:
		return False, [
			"No Employee record is readable for the signed-in user, so no reporting tree "
			"can be resolved. Returning the empty team contract. See GAP-3."
		]

	if team:
		return True, []

	if privileged:
		return False, [
			f"Employee '{manager.name}' holds {sorted(roles & set(MANAGER_ROLES))} but has no "
			"reports, so there is no team to show. Org-wide figures belong to the HR "
			"Operations tab, which is out of scope here. See GAP-3."
		]

	return False, [
		f"Employee '{manager.name}' has no reports and holds none of {list(MANAGER_ROLES)}; "
		"not authorized for the Team Dashboard. Returning the empty team contract. See GAP-3."
	]


# ---------------------------------------------------------------------------
# team-wide data access -- ONE query per widget for the whole team
# ---------------------------------------------------------------------------


def _fetch_team_attendance(team_ids, start, end):
	"""Submitted attendance for every team member in the window. Single query."""
	if not team_ids or not _can("Attendance"):
		return None
	return frappe.get_list(
		"Attendance",
		filters={
			"employee": ["in", team_ids],
			"docstatus": 1,
			"attendance_date": ["between", [start, end]],
		},
		fields=["employee", "employee_name", "attendance_date", "status", "leave_type"],
		order_by="attendance_date asc",
		limit_page_length=0,
	)


def _fetch_team_approved_leaves(team_ids, start, end):
	"""Approved leave overlapping the window, whole team. Single query."""
	if not team_ids or not _can("Leave Application"):
		return []
	return frappe.get_list(
		"Leave Application",
		filters={
			"employee": ["in", team_ids],
			"docstatus": 1,
			"status": "Approved",
			"from_date": ["<=", end],
			"to_date": [">=", start],
		},
		fields=["employee", "employee_name", "leave_type", "from_date", "to_date"],
		limit_page_length=0,
	)


def _calendar_facts(holiday_list, holiday_rows, start, end):
	"""(weekend_dates, public_holiday_dates, source, detail) for ONE Holiday List.

	Weekends come from my_dashboard._weekend_info, which cross-checks the list instead of
	trusting it: a list marking only Sundays previously made every Saturday a working day
	(July 2026 reported 4 weekends instead of 8).
	"""
	weekend_dates, source, detail = _weekend_info(holiday_rows, holiday_list, start, end)
	public_dates = {getdate(h.holiday_date) for h in holiday_rows if not cint(h.weekly_off)}
	return weekend_dates, public_dates, source, detail


def _holidays_by_member(team, start, end):
	"""Holiday rows per member, fetching each distinct Holiday List only once.

	Members can sit on different lists, so this groups by list rather than looping
	per employee.
	"""
	by_list = {}
	for m in team:
		by_list.setdefault(m.get("holiday_list"), []).append(m.name)

	cache = {}
	out = {}
	for hl, members in by_list.items():
		if hl not in cache:
			cache[hl] = _fetch_holidays(hl, start, end) if hl else []
		for emp in members:
			out[emp] = cache[hl]
	return out


# ---------------------------------------------------------------------------
# WIDGETS 1-4 : KPI CARDS
# mock `s` @1808900:
#   {label:"Team size",         value:"12 members", subtitle:<span>"+20"</span>}
#   {label:"Present today",     value:"10"}
#   {label:"Pending approvals", value:"5",  cardBorder:"#ff9595ff"}
#   {label:"On leave today",    value:"2",  cardBorder:"#ffd48aff"}
# `value` is a preformatted string; "Team size" carries the " members" suffix.
# The "+20" badge is the same undefined literal as My Dashboard -- emitted as null.
# ---------------------------------------------------------------------------


def _build_kpis(ctx):
	team_size = len(ctx.team)
	present_today = on_leave_today = 0

	if ctx.team_ids and _can("Attendance"):
		today_ = getdate(nowdate())
		rows = frappe.get_list(
			"Attendance",
			filters={
				"employee": ["in", ctx.team_ids],
				"docstatus": 1,
				"attendance_date": today_,
			},
			fields=["employee", "status"],
			limit_page_length=0,
		)
		seen_present, seen_leave = set(), set()
		for r in rows:
			if r.status in ("Present", "Work From Home"):
				seen_present.add(r.employee)
			elif r.status == "On Leave":
				seen_leave.add(r.employee)
			elif r.status == "Half Day":
				seen_present.add(r.employee)
		# Approved leave with no Attendance row still counts as on leave today.
		if _can("Leave Application"):
			for r in frappe.get_list(
				"Leave Application",
				filters={
					"employee": ["in", ctx.team_ids],
					"docstatus": 1,
					"status": "Approved",
					"from_date": ["<=", today_],
					"to_date": [">=", today_],
				},
				fields=["employee"],
				limit_page_length=0,
			):
				seen_leave.add(r.employee)
		present_today = len(seen_present)
		on_leave_today = len(seen_leave - seen_present)

	pending_total = ctx.pending_approvals_total

	return [
		{
			"label": "Team size",
			"value": f"{team_size} members",
			"count": team_size,
			"scope": ctx.scope,
			"badge": None,  # GAP-1: "+20" meaning undefined
			"permitted": bool(ctx.team_ids),
		},
		{
			"label": "Present today",
			"value": cstr(present_today),
			"count": present_today,
			"permitted": bool(ctx.team_ids) and _can("Attendance"),
		},
		{
			"label": "Pending approvals",
			"value": cstr(pending_total),
			"count": pending_total,
			"permitted": bool(ctx.team_ids),
		},
		{
			"label": "On leave today",
			"value": cstr(on_leave_today),
			"count": on_leave_today,
			"permitted": bool(ctx.team_ids),
		},
	]


# ---------------------------------------------------------------------------
# WIDGET 5 : TEAM CALENDAR
# mock `c` @1810600 -- built per member, then FLATTENED to one entry per member per day:
#   {id:1, title:"Present", name:"Rahul", date:new Date(2026,6,1), type:"present"}
#   types: present | wfh | leave | absent   (title: Present | Work From Home | On Leave | Absent)
#   `name` is the member's FIRST name.
# mock `d` @1812540 -- stats derived by counting `c`, exactly 4 entries in this order:
#   [{label:"Present",value:N,type:"present"}, {label:"On Leave",…,"leave"},
#    {label:"WFH",…,"wfh"}, {label:"Absent",…,"absent"}]
# Calendar is rendered with variant="team", which groups entries per day itself and
# renders the "+N more" affordance -- so individual entries are the contract, NOT
# pre-aggregated day rows. Per-day counts are additionally supplied under `by_date`.
# ---------------------------------------------------------------------------


def _build_team_calendar(ctx, include_unmarked=1):
	"""SHAPE: `data` is ONE CELL PER MEMBER PER DAY -- verified against the bundle, not
	assumed.

	The team variant of DashboardCalendarView renders a per-PERSON chip and rolls the rest
	of that day up as "+N More":

	    o === "team" ? `${Y.name.split(" ")[0]} (${Y.type === "present" ? "P" : ...})`
	    j.length > 1 && <span>+{j.length - 1} More</span>

	`j` is that day's filtered slice of `data`, so BOTH the chip label and the roll-up
	count are derived from per-person entries. The expanded day panel goes further:
	`Y[type].push(cell)` … `children: se.length` … `se.map(le => "By: " + le.name)`.

	Collapsing `data` to one aggregate cell per day would therefore make `j.length` always
	1 -- "+N More" would never render, the chip would lose the member's name, and the
	per-type counts in the detail panel would all read 1. The mock agrees: it flattens
	per-member day arrays into ~170 entries for 8 members.

	Payload is O(days x members) BY DESIGN. The component does not paginate; it re-runs its
	map+filter over the whole array for each day cell, i.e. O(days^2 x members) of client
	work. `totals.cells` and the warning below make that visible, and include_unmarked=0
	removes the dominant term when attendance is sparse.
	"""
	include_unmarked = cint(include_unmarked)
	empty = {
		"month": ctx.month,
		"year": ctx.year,
		"data": [],
		"stats": _team_stats(0, 0, 0, 0),
		"by_date": {},
		"totals": {},
		"permitted": False,
	}
	if not ctx.team_ids or ctx.att_rows is None:
		return empty

	holiday_list, weekend_dates, public_dates, weekend_source, weekend_detail = ctx.team_facts
	names = {m.name: _first_name(m.employee_name) for m in ctx.team}
	baseline_holiday_names = ctx.baseline_holiday_names

	# Index attendance and approved leave by (date -> rows). Previously the calendar was
	# built ONLY from these two collections, so a month with no attendance returned
	# data:[] and by_date:{} even though its weekends and holidays were known. The day
	# loop below now always runs across the window.
	att_by_date = {}
	for r in ctx.att_rows:
		att_by_date.setdefault(getdate(r.attendance_date), []).append(r)

	leave_by_date = {}
	for lv in ctx.approved_leaves:
		d = max(getdate(lv.from_date), ctx.start)
		last = min(getdate(lv.to_date), ctx.win_end)
		while d <= last:
			leave_by_date.setdefault(d, {})[lv.employee] = lv
			d += datetime.timedelta(days=1)

	events = []
	by_date = {}
	present = wfh = leave = absent = notmarked = 0
	weekend_days = holiday_days = 0
	today_ = getdate(nowdate())

	cur = ctx.start
	while cur <= ctx.win_end:
		is_holiday = cur in public_dates
		is_weekend = (not is_holiday) and cur in weekend_dates
		slot = {
			"present": 0,
			"on_leave": 0,
			"wfh": 0,
			"absent": 0,
			# additive: lets the UI shade non-working days without a second call
			"not_marked": 0,
			"is_weekend": is_weekend,
			"is_holiday": is_holiday,
			"holiday_name": None,
		}

		if is_holiday:
			holiday_days += 1
			label = baseline_holiday_names.get(cur) or "Holiday"
			slot["holiday_name"] = label
			# One team-level entry so the day renders; the team calendar variant styles
			# `holiday`. Weekends deliberately get no entry (as in the mock).
			events.append(
				{
					"id": 0,
					"title": label,
					"name": "National Holiday",
					"date": cur.isoformat(),
					"type": "holiday",
				}
			)
		elif is_weekend:
			weekend_days += 1

		seen = set()
		for r in att_by_date.get(cur, []):
			seen.add(r.employee)
			etype = TEAM_TYPE_MAP.get(r.status) or "present"
			events.append(
				{
					"id": 0,
					"title": TEAM_TITLE_MAP[etype],
					"name": names.get(r.employee) or _first_name(r.employee_name),
					"date": cur.isoformat(),
					"type": etype,
					"employee": r.employee,
					"employee_name": r.employee_name,
				}
			)
			if etype == "present":
				present += 1
				slot["present"] += 1
			elif etype == "wfh":
				wfh += 1
				slot["wfh"] += 1
			elif etype == "leave":
				leave += 1
				slot["on_leave"] += 1
			else:
				absent += 1
				slot["absent"] += 1

		for emp, lv in (leave_by_date.get(cur) or {}).items():
			if emp in seen:
				continue
			events.append(
				{
					"id": 0,
					"title": TEAM_TITLE_MAP["leave"],
					"name": names.get(emp) or _first_name(lv.employee_name),
					"date": cur.isoformat(),
					"type": "leave",
					"employee": emp,
					"employee_name": lv.employee_name,
				}
			)
			leave += 1
			slot["on_leave"] += 1

		# A past working day where a member has neither attendance nor approved leave.
		# Without these cells a month with no attendance rendered a completely blank
		# calendar (July 2026: data:[] while by_date held all 31 days). My Dashboard fills
		# the same gap with "notmarked", so both calendars now behave alike.
		if include_unmarked and not is_holiday and not is_weekend and cur <= today_:
			for m in ctx.team:
				if m.name in seen or m.name in (leave_by_date.get(cur) or {}):
					continue
				doj = getdate(m.get("date_of_joining")) if m.get("date_of_joining") else None
				if doj and cur < doj:
					continue
				events.append(
					{
						"id": 0,
						"title": "Not marked",
						"name": names.get(m.name) or _first_name(m.employee_name),
						"date": cur.isoformat(),
						"type": "notmarked",
						"employee": m.name,
						"employee_name": m.employee_name,
					}
				)
				notmarked += 1
				slot["not_marked"] = slot.get("not_marked", 0) + 1

		by_date[cur.isoformat()] = slot
		cur += datetime.timedelta(days=1)

	events.sort(key=lambda e: (e["date"], e["type"], e["name"]))
	for i, e in enumerate(events, start=1):
		e["id"] = i

	# The component re-filters the full array per day cell, so a large team makes the
	# client do O(days^2 x members) work. Surface it rather than letting it degrade quietly.
	if len(events) > 1500:
		ctx.warnings.append(
			f"Team calendar returned {len(events)} cells for {len(ctx.team_ids)} members "
			f"({ctx.month:02d}/{ctx.year}). The calendar component renders one chip per member "
			"per day and does not paginate. Pass include_unmarked=0 to drop unmarked-day "
			"cells, or narrow the team with scope=direct."
		)

	days_elapsed = max((ctx.win_end - ctx.start).days + 1, 0)

	return {
		"month": ctx.month,
		"year": ctx.year,
		"data": events,
		# contract: exactly the mock's 4 entries, in the mock's order. The bundle counts
		# type==="present" EXCLUSIVELY (WFH is its own stat), so present must not absorb wfh.
		"stats": _team_stats(
			present, leave, wfh, absent, holiday_days, weekend_days, notmarked
		),
		"by_date": by_date,
		"totals": {
			"present": present,
			"on_leave": leave,
			"wfh": wfh,
			"absent": absent,
			"not_marked": notmarked,
			"team_size": len(ctx.team_ids),
			"days_in_month": (ctx.end - ctx.start).days + 1,
			# additive, mirroring my_dashboard
			"weekends": weekend_days,
			"holidays": holiday_days,
			"days_elapsed": days_elapsed,
			"working_days": max(days_elapsed - weekend_days - holiday_days, 0),
			"window": ctx.window_policy,
			"window_end": ctx.win_end.isoformat(),
			"holiday_list": holiday_list,
			"weekend_source": weekend_source,
			"weekend_detail": weekend_detail,
			# additive scaling diagnostics -- `data` is O(days x members) by contract
			"cells": len(events),
			"max_cells_per_day": max(
				(
					v["present"] + v["on_leave"] + v["wfh"] + v["absent"] + v["not_marked"]
					for v in by_date.values()
				),
				default=0,
			),
			"include_unmarked": bool(include_unmarked),
		},
		"permitted": True,
	}


def _team_stats(present, leave, wfh, absent, holidays=0, weekends=0, notmarked=0):
	"""Legend for the calendar. `stats` IS the legend: the component renders it as
	clickable chips (`B.map`) and clicking one filters `data` by that chip's `type`.

	The mock's 4 entries stay first and in the mock's order; Holidays / Weekends /
	Not marked are APPENDED so the legend summarises the month the way My Dashboard's
	does instead of reading all-zeros.

	CAVEAT (identical in My Dashboard, kept for consistency): weekends deliberately emit
	no `data` cells, so the Weekends chip shows a count but filters to nothing.
	"""
	return [
		{"label": "Present", "value": _num(present), "type": "present"},
		{"label": "On Leave", "value": _num(leave), "type": "leave"},
		{"label": "WFH", "value": _num(wfh), "type": "wfh"},
		{"label": "Absent", "value": _num(absent), "type": "absent"},
		{"label": "Holidays", "value": _num(holidays), "type": "holiday"},
		{"label": "Weekends", "value": _num(weekends), "type": "weekend"},
		{"label": "Not marked", "value": _num(notmarked), "type": "notmarked"},
	]


# ---------------------------------------------------------------------------
# WIDGET 6 : PENDING APPROVALS
# mock `o` @1809900 -- exactly THREE keys (component `eQ` reads only these):
#   {initials:"RS", name:"Rahul Sharma", detail:"Casual Leave  ·  Apr 14 – Apr 15"}
#   {initials:"PP", name:"Priya Patel",  detail:"Expense Claim  ·  ₹3,200 · Travel"}
#   {initials:"AM", name:"Ankit Mehta",  detail:"WFH Request  ·  Apr 12"}
#   {initials:"SJ", name:"Sneha Joshi",  detail:"Compensatory Off  ·  1 day"}
#   {initials:"VK", name:"Vivek Kumar",  detail:"Attendance Regularization  ·  Apr 10"}
#
# CONTRACT HAZARD: the sub-tabs filter client-side on detail.toLowerCase() --
#   Leaves -> "leave" | Expenses -> "expense"|"claim" | WFH -> "wfh"|"off"|"regularization"
# The detail strings below keep those keywords, and SEP reproduces the mock's
# two-space middot. `employee_name`/`type`/`subtitle`/`date_or_range`/`category` from the
# brief are emitted as ADDITIVE keys.
#
# "Awaiting THIS manager" is enforced as: pending state AND employee IN team.
# Leave Application and Expense Claim additionally carry a named approver, surfaced as
# `is_named_approver`; Attendance Request and Compensatory Leave Request have NO approver
# field at all (verified in hrms 15.49.2), so team membership is the only signal. See GAP-4.
# ---------------------------------------------------------------------------


def _build_pending_approvals(ctx, category=None, limit=10):
	if not ctx.team_ids:
		return {"data": [], "total": 0, "counts": {}, "permitted": False}

	wanted = _resolve_categories(category)
	limit = max(1, min(cint(limit) or 10, 100))
	user = frappe.session.user
	team_ids = ctx.team_ids
	items = []

	# `employee_name` is denormalised onto each transaction at creation time, so a later
	# Employee rename leaves it stale. Prefer the live name from the resolved team set
	# (already in memory) so this list agrees with the calendar and members table.
	live_names = {m.name: m.employee_name for m in ctx.team}

	if CATEGORY_LEAVE in wanted and _can("Leave Application"):
		for r in frappe.get_list(
			"Leave Application",
			filters={"employee": ["in", team_ids], "docstatus": 0, "status": "Open"},
			fields=[
				"name",
				"employee",
				"employee_name",
				"leave_type",
				"from_date",
				"to_date",
				"total_leave_days",
				"leave_approver",
				"modified",
			],
			order_by="modified desc",
			limit_page_length=limit * 3,
		):
			rng = _daterange_label(r.from_date, r.to_date)
			# keeps the "leave" keyword for the Leaves sub-tab
			label = r.leave_type or "Leave"
			if "leave" not in label.lower():
				label = f"{label} Leave"
			items.append(
				_approval_row(
					r, label, rng, CATEGORY_LEAVE, "Leave Application", user, r.leave_approver,
					live_names=live_names,
				)
			)

	if CATEGORY_EXPENSE in wanted and _can("Expense Claim"):
		currency = _company_currency(ctx.company)
		for r in frappe.get_list(
			"Expense Claim",
			filters={
				"employee": ["in", team_ids],
				"docstatus": ["<", 2],
				"approval_status": "Draft",
			},
			fields=[
				"name",
				"employee",
				"employee_name",
				"total_claimed_amount",
				"expense_approver",
				"posting_date",
				"modified",
			],
			order_by="modified desc",
			limit_page_length=limit * 3,
		):
			items.append(
				_approval_row(
					r,
					"Expense Claim",  # keeps "expense" + "claim"
					_fmt_money(r.total_claimed_amount, currency),
					CATEGORY_EXPENSE,
					"Expense Claim",
					user,
					r.expense_approver,
					amount=flt(r.total_claimed_amount, 2),
					live_names=live_names,
				)
			)

	if _can("Attendance Request"):
		# reason is a Select of exactly "Work From Home|On Duty"; docstatus 0 == pending.
		for r in frappe.get_list(
			"Attendance Request",
			filters={"employee": ["in", team_ids], "docstatus": 0},
			fields=[
				"name",
				"employee",
				"employee_name",
				"from_date",
				"to_date",
				"reason",
				"modified",
			],
			order_by="modified desc",
			limit_page_length=limit * 3,
		):
			is_wfh = r.reason == "Work From Home"
			cat = CATEGORY_WFH if is_wfh else CATEGORY_REGULARIZATION
			if cat not in wanted:
				continue
			items.append(
				_approval_row(
					r,
					# "WFH Request" keeps "wfh"; "Attendance Regularization" keeps
					# "regularization" -- both land in the WFH sub-tab, as in the mock.
					"WFH Request" if is_wfh else "Attendance Regularization",
					_daterange_label(r.from_date, r.to_date),
					cat,
					"Attendance Request",
					user,
					None,
					live_names=live_names,
				)
			)

	if CATEGORY_COMP_OFF in wanted and _can("Compensatory Leave Request"):
		for r in frappe.get_list(
			"Compensatory Leave Request",
			filters={"employee": ["in", team_ids], "docstatus": 0},
			fields=[
				"name",
				"employee",
				"employee_name",
				"work_from_date",
				"work_end_date",
				"modified",
			],
			order_by="modified desc",
			limit_page_length=limit * 3,
		):
			days = 1
			if r.work_from_date and r.work_end_date:
				days = (getdate(r.work_end_date) - getdate(r.work_from_date)).days + 1
			items.append(
				_approval_row(
					r,
					"Compensatory Off",  # keeps "off" -> WFH sub-tab, as in the mock
					f"{days} day" if days == 1 else f"{days} days",
					CATEGORY_COMP_OFF,
					"Compensatory Leave Request",
					user,
					None,
					date_or_range=_daterange_label(r.work_from_date, r.work_end_date),
					live_names=live_names,
				)
			)

	items.sort(key=lambda x: x.pop("_sort") or "", reverse=True)

	counts = {c: 0 for c in CATEGORIES}
	for it in items:
		counts[it["category"]] += 1

	return {
		"data": items[:limit],
		"total": len(items),
		"counts": counts,
		"permitted": True,
	}


def _approval_row(
	r, label, tail, category, doctype, user, approver, amount=None, date_or_range=None,
	live_names=None
):
	detail = f"{label}{SEP}{tail}" if tail else label
	name = (live_names or {}).get(r.employee) or r.employee_name
	row = {
		# exactly the 3 keys the UI reads
		"initials": _initials(name),
		"name": name,
		"detail": detail,
		# additive
		"employee": r.employee,
		"employee_name_on_doc": r.employee_name,
		"type": label,
		"subtitle": tail,
		"date_or_range": date_or_range if date_or_range is not None else tail,
		"category": category,
		"doctype": doctype,
		"docname": r.name,
		"is_named_approver": bool(approver) and approver == user,
		"_sort": r.modified,
	}
	if amount is not None:
		row["amount"] = amount
	return row


def _resolve_categories(category):
	"""Accepts a category, a UI sub-tab label ("All"/"Leaves"/"Expenses"/"WFH"), or None."""
	if not category:
		return set(CATEGORIES)
	key = cstr(category).strip().lower()
	if key in SUBTAB_CATEGORIES:
		return set(SUBTAB_CATEGORIES[key])
	if key in CATEGORIES:
		return {key}
	frappe.throw(
		_("`category` must be one of {0} or a sub-tab label {1}.").format(
			", ".join(CATEGORIES), ", ".join(sorted(SUBTAB_CATEGORIES))
		)
	)


# ---------------------------------------------------------------------------
# WIDGET 7 : TEAM MEMBERS TABLE  (has its OWN month picker)
# mock `l` @1810100 -- counters NESTED under `data`, camelCase:
#   {initials:"RS", name:"Rahul",
#    data:{payableDays:18, present:17, absent:1, onLeave:2, holidays:1, weekends:8}}
#   `name` is the FIRST name only.
# The picker offers "January 2026" … "December 2026"; `month`/`year` params drive it
# independently of the calendar widget.
#
# Aggregated with ONE grouped pass over the team's attendance rows -- no per-member query.
# ---------------------------------------------------------------------------


def _build_team_members_summary(ctx, month=None, year=None):
	if not ctx.team_ids:
		return {"data": [], "month": ctx.month, "year": ctx.year, "permitted": False}

	# This widget's month is independent of the calendar's. The window POLICY is the
	# same either way (month_to_date for the current month unless full_month=1).
	own_window = bool(month or year)
	if own_window:
		start, end, month, year = _month_window(month, year)
		win_end, window_policy = _window_end(
			start, end, ctx.window_policy == WINDOW_FULL_MONTH
		)
		att_rows = _fetch_team_attendance(ctx.team_ids, start, win_end)
		leaves = _fetch_team_approved_leaves(ctx.team_ids, start, win_end)
		holidays = _holidays_by_member(ctx.team, start, win_end)
		facts = {}
		for m in ctx.team:
			hl = m.get("holiday_list")
			if hl not in facts:
				facts[hl] = _calendar_facts(hl, holidays.get(m.name) or [], start, win_end)
	else:
		start, end, month, year = ctx.start, ctx.end, ctx.month, ctx.year
		win_end, window_policy = ctx.win_end, ctx.window_policy
		att_rows, leaves = ctx.att_rows, ctx.approved_leaves
		holidays = ctx.holidays_by_member
		facts = ctx.facts_by_list

	if att_rows is None:
		return {"data": [], "month": month, "year": year, "permitted": False}

	window_days = max((win_end - start).days + 1, 0)

	lwp = ctx.lwp_types
	agg = {
		m.name: {
			"present": 0,
			"wfh": 0,
			"absent": 0,
			"on_leave": 0,
			"half_day": 0,
			"paid_leave": 0,
			"unpaid_leave": 0,
		}
		for m in ctx.team
	}
	covered = set()

	for r in att_rows:
		a = agg.get(r.employee)
		if a is None:
			continue
		covered.add((r.employee, getdate(r.attendance_date)))
		if r.status == "Present":
			a["present"] += 1
		elif r.status == "Work From Home":
			a["wfh"] += 1
		elif r.status == "Absent":
			a["absent"] += 1
		elif r.status == "Half Day":
			a["half_day"] += 1
		elif r.status == "On Leave":
			a["on_leave"] += 1
			if r.leave_type and r.leave_type in lwp:
				a["unpaid_leave"] += 1
			else:
				a["paid_leave"] += 1

	for lv in leaves:
		a = agg.get(lv.employee)
		if a is None:
			continue
		d, last = max(getdate(lv.from_date), start), min(getdate(lv.to_date), win_end)
		while d <= last:
			if (lv.employee, d) not in covered:
				covered.add((lv.employee, d))
				a["on_leave"] += 1
				if lv.leave_type and lv.leave_type in lwp:
					a["unpaid_leave"] += 1
				else:
					a["paid_leave"] += 1
			d += datetime.timedelta(days=1)

	out = []
	weekend_sources = set()
	for m in ctx.team:
		a = agg[m.name]
		weekend_dates, public_dates, wsource, _wdetail = facts.get(
			m.get("holiday_list")
		) or (set(), set(), WEEKEND_FROM_LIST, {})
		weekend_sources.add(wsource)
		# Reconciled weekends (weekly_off weekdays + Sat/Sun supplement), not raw
		# Holiday List rows -- a Sunday-only list previously reported 4 for July.
		#
		# Mutually exclusive, exactly as the team calendar classifies them: a public
		# holiday that also falls on a weekend counts ONCE, as a holiday. Counting both
		# sets independently deducted such a day twice (Aug 15 2026 is a Saturday AND
		# Independence Day, which produced payableDays 20 instead of 21).
		public = len([d for d in public_dates if start <= d <= win_end])
		weekends = len([d for d in weekend_dates - public_dates if start <= d <= win_end])
		# payableDays = payable days AVAILABLE in the window, i.e. working days. This is
		# what the mock shows (every member has the same 18 while present/absent vary).
		payable = max(window_days - weekends - public, 0)

		out.append(
			{
				"initials": _initials(m.employee_name),
				"name": _first_name(m.employee_name),
				"data": {
					"payableDays": _num(payable),
					"present": _num(a["present"] + a["wfh"]),
					"absent": _num(a["absent"]),
					"onLeave": _num(a["on_leave"]),
					"holidays": _num(public),
					"weekends": _num(weekends),
				},
				# additive
				"employee": m.name,
				"employee_name": m.employee_name,
				"designation": m.get("designation"),
				"image": m.get("image"),
				"breakdown": {
					"present": a["present"],
					"wfh": a["wfh"],
					"half_day": a["half_day"],
					"paid_leave": a["paid_leave"],
					"unpaid_leave": a["unpaid_leave"],
				},
			}
		)

	return {
		"data": out,
		"month": month,
		"year": year,
		# additive
		"window": window_policy,
		"window_end": win_end.isoformat(),
		"window_days": window_days,
		"weekend_source": sorted(weekend_sources)[0] if weekend_sources else WEEKEND_FROM_LIST,
		"permitted": True,
	}


# ---------------------------------------------------------------------------
# request context -- team resolved ONCE, data fetched lazily so a cache hit
# never touches the database
# ---------------------------------------------------------------------------


class _Ctx:
	__slots__ = (
		"manager",
		"manager_id",
		"company",
		"scope",
		"team",
		"team_ids",
		"authorized",
		"warnings",
		"start",
		"end",
		"win_end",
		"window_policy",
		"month",
		"year",
		"_memo",
	)

	def __init__(self, month=None, year=None, scope="subtree", full_month=False):
		manager, warnings = _resolve_employee()
		self.manager = manager
		self.manager_id = manager.name if manager else None
		self.company = manager.company if manager else None
		self.scope = "direct" if cstr(scope).lower() == "direct" else "subtree"
		self.warnings = list(warnings)
		self.start, self.end, self.month, self.year = _month_window(month, year)
		# Same policy as My Dashboard: current month is capped at today unless
		# full_month=1. Applied to the members table AND the team calendar; the
		# point-in-time KPIs (Present today / On leave today / Pending approvals) are
		# deliberately NOT windowed -- they always mean "today".
		self.win_end, self.window_policy = _window_end(self.start, self.end, full_month)
		self._memo = {}

		team, tree_total = ([], 0)
		if manager:
			team, tree_total = _fetch_team(manager, self.scope)
			if team is None:
				team = []
				self.warnings.append("No read permission on Employee; team is empty.")
			elif tree_total > len(team):
				self.warnings.append(
					f"{tree_total} employees are in this manager's reporting {self.scope} but "
					f"only {len(team)} are readable. A User Permission on Employee is "
					"restricting the manager's own view -- add User Permissions for the "
					"reports, or remove the manager's self-restriction. See GAP-6."
				)

		self.team = team
		self.team_ids = [m.name for m in team]
		self.authorized, auth_warnings = _authorize(manager, team)
		self.warnings.extend(auth_warnings)

	@property
	def month_key(self):
		return f"{self.year}-{self.month:02d}"

	@property
	def cache_extra(self):
		# window policy is part of the key: month_to_date and full_month payloads differ
		return f"{self.scope}:{self.month_key}:{self.window_policy}"

	@lazy
	def att_rows(self):
		return _fetch_team_attendance(self.team_ids, self.start, self.win_end)

	@lazy
	def approved_leaves(self):
		return _fetch_team_approved_leaves(self.team_ids, self.start, self.win_end)

	@lazy
	def holidays_by_member(self):
		return _holidays_by_member(self.team, self.start, self.win_end)

	@lazy
	def facts_by_list(self):
		"""{holiday_list: (weekend_dates, public_dates, source, detail)}.

		Computed once per DISTINCT Holiday List, not per member -- members frequently
		share a list, and _weekend_info walks the window.
		"""

		out = {}
		for m in self.team:
			hl = m.get("holiday_list")
			if hl in out:
				continue
			rows = self.holidays_by_member.get(m.name) or []
			out[hl] = _calendar_facts(hl, rows, self.start, self.win_end)
		return out

	def member_facts(self, member):
		"""Weekend/holiday facts for one member's own Holiday List."""
		return self.facts_by_list.get(member.get("holiday_list")) or (set(), set(), WEEKEND_FROM_LIST, {})

	@property
	def baseline_holiday_names(self):
		"""{date: label} for the team baseline list -- filled in by `team_facts`."""
		self.team_facts  # force build
		return self._memo.get("baseline_holiday_names") or {}

	@lazy
	def team_facts(self):
		"""Baseline calendar facts for the TEAM calendar.

		A team calendar needs ONE shared notion of "holiday", so it uses the Holiday List
		most of the team is actually on (modal, ties broken by name for determinism) --
		NOT the manager's. The manager is frequently an outlier: on this bench they sit on
		a 2025 list with no rows in the requested month, which made every public holiday
		vanish from the team calendar. Individual member counters in the members table
		still use each member's own list.
		"""

		counts = {}
		for m in self.team:
			hl = m.get("holiday_list")
			if hl:
				counts[hl] = counts.get(hl, 0) + 1
		if counts:
			hl = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
		elif self.manager:
			hl = self.manager.get("holiday_list") or get_holiday_list_for_employee(
				self.manager.name, raise_exception=False
			)
		else:
			hl = None
		rows = _fetch_holidays(hl, self.start, self.win_end) if hl else []
		# Stash the labels so the calendar reuses these rows instead of refetching.
		self._memo["baseline_holiday_names"] = {
			getdate(h.holiday_date): (
				strip_html(cstr(h.description or "")).strip() or "Holiday"
			)
			for h in rows
			if not cint(h.weekly_off)
		}
		facts = _calendar_facts(hl, rows, self.start, self.win_end)
		if facts[2] == WEEKEND_RECONCILED:
			self.warnings.append(
				f"Holiday List '{hl or '(none)'}' marks weekly offs only on "
				f"{', '.join(facts[3].get('weekly_off_weekdays') or []) or '(none)'}; added "
				f"{', '.join(facts[3].get('supplemented_with') or [])} from the calendar so "
				"those days are not counted as working days."
			)
		elif facts[2] == WEEKEND_FROM_WEEKDAY:
			self.warnings.append(
				f"Holiday List '{hl or '(none)'}' has no weekly_off rows in "
				f"{self.year}-{self.month:02d}; weekends were derived from the calendar."
			)
		return (hl,) + facts

	@lazy
	def lwp_types(self):
		return _lwp_leave_types()

	@lazy
	def pending_approvals_total(self):
		"""Shared by KPI #3 and widget #6 so the two can never disagree."""
		return _build_pending_approvals(self, None, 100)["total"]

	def meta(self, **extra):
		meta = {
			"manager": self.manager_id,
			"manager_name": self.manager.employee_name if self.manager else None,
			"employee_linked": bool(self.manager),
			"authorized": self.authorized,
			"scope": self.scope,
			"team_size": len(self.team_ids),
			"company": self.company,
			"month": self.month,
			"year": self.year,
			"from_date": self.start.isoformat(),
			"to_date": self.end.isoformat(),
			# Members table + team calendar use this capped window. The point-in-time
			# KPIs are independent of it.
			"attendance_window": {
				"policy": self.window_policy,
				"from_date": self.start.isoformat(),
				"to_date": self.win_end.isoformat(),
				"days": max((self.win_end - self.start).days + 1, 0),
			},
			"generated_on": frappe.utils.now(),
		}
		if self.warnings:
			meta["warnings"] = self.warnings
		meta.update(extra)
		return meta


def _empty_payload(ctx):
	return {
		"kpis": [
			{"label": "Team size", "value": "0 members", "count": 0, "scope": ctx.scope, "badge": None, "permitted": False},
			{"label": "Present today", "value": "0", "count": 0, "permitted": False},
			{"label": "Pending approvals", "value": "0", "count": 0, "permitted": False},
			{"label": "On leave today", "value": "0", "count": 0, "permitted": False},
		],
		"team_calendar": {
			"month": ctx.month,
			"year": ctx.year,
			"data": [],
			"stats": _team_stats(0, 0, 0, 0),
			"by_date": {},
			"totals": {},
			"permitted": False,
		},
		"pending_approvals": {"data": [], "total": 0, "counts": {}, "permitted": False},
		"team_members": {"data": [], "month": ctx.month, "year": ctx.year, "permitted": False},
	}


def _w(widget, ctx, extra, builder):
	"""Per-widget cache, keyed by widget + manager + scope + month."""
	return _cached(WIDGET.format(widget), ctx.manager_id, extra, builder)


# ---------------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------------


@endpoint
def get_team_dashboard(
	month=None,
	year=None,
	scope="subtree",
	approvals_limit=10,
	members_month=None,
	members_year=None,
	full_month=0,
	include_unmarked=1,
):
	"""Page load: every widget on the Team Dashboard tab in one round trip.

	The team set is resolved once and reused by all four widgets.
	`members_month`/`members_year` drive the members table independently.
	"""
	ctx = _Ctx(month, year, scope, full_month)
	if not ctx.authorized:
		return _ok(_empty_payload(ctx), ctx.meta(tab="Team Dashboard"))

	mx = ctx.cache_extra
	members_extra = (
		f"{ctx.scope}:{cint(members_year) or ctx.year}-{cint(members_month) or ctx.month:02d}"
	)
	return _ok(
		{
			"kpis": _w("kpis", ctx, mx, lambda: _build_kpis(ctx)),
			"team_calendar": _w(
				"calendar",
				ctx,
				f"{mx}:u{cint(include_unmarked)}",
				lambda: _build_team_calendar(ctx, include_unmarked),
			),
			"pending_approvals": _w(
				"approvals",
				ctx,
				f"{ctx.scope}:{nowdate()}:{cint(approvals_limit)}",
				lambda: _build_pending_approvals(ctx, None, approvals_limit),
			),
			"team_members": _w(
				"members",
				ctx,
				members_extra,
				lambda: _build_team_members_summary(ctx, members_month, members_year),
			),
		},
		ctx.meta(tab="Team Dashboard"),
	)


@endpoint
def get_team_calendar(month=None, year=None, scope="subtree", full_month=0, include_unmarked=1):
	"""Team calendar month navigation."""
	ctx = _Ctx(month, year, scope, full_month)
	if not ctx.authorized:
		return _ok(_empty_payload(ctx)["team_calendar"], ctx.meta())
	return _ok(
		_w(
			"calendar",
			ctx,
			f"{ctx.cache_extra}:u{cint(include_unmarked)}",
			lambda: _build_team_calendar(ctx, include_unmarked),
		),
		ctx.meta(),
	)


@endpoint
def get_pending_approvals(category=None, limit=10, scope="subtree"):
	"""Approvals sub-tabs (All / Leaves / Expenses / WFH) and "View all".

	`category` accepts a UI sub-tab label or a raw category
	(leave | expense | wfh | comp_off | regularization).
	"""
	ctx = _Ctx(None, None, scope)
	if not ctx.authorized:
		return _ok(_empty_payload(ctx)["pending_approvals"], ctx.meta())
	return _ok(
		_w(
			"approvals",
			ctx,
			f"{ctx.scope}:{nowdate()}:{cstr(category).lower()}:{cint(limit)}",
			lambda: _build_pending_approvals(ctx, category, limit),
		),
		ctx.meta(category=cstr(category) or "all"),
	)


@endpoint
def get_team_members_summary(month=None, year=None, scope="subtree", full_month=0):
	"""Team members table -- driven by its own month picker."""
	ctx = _Ctx(month, year, scope, full_month)
	if not ctx.authorized:
		return _ok(_empty_payload(ctx)["team_members"], ctx.meta())
	return _ok(
		_w(
			"members",
			ctx,
			ctx.cache_extra,
			lambda: _build_team_members_summary(ctx),
		),
		ctx.meta(),
	)
