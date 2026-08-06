# Copyright (c) 2026, Flitz Interactive and contributors
# For license information, please see license.txt
"""
Read-only API for the "My Dashboard" tab of /vnphrms/dashboard (Home).

SCOPE
-----
Self-service. Every query is scoped to the Employee linked to the API-key user via
`Employee.user_id`. The other tabs on this route (Team / HR Operations / System) are
not implemented here.

PERMISSIONS
-----------
All reads go through `frappe.get_list`, so Frappe applies role permissions AND User
Permissions itself -- this API grants no privilege of its own. On top of that:
  * every transactional filter is pinned to the resolved `employee`, and
  * each doctype is gated by `frappe.has_permission(dt, "read")` so a missing role
    degrades one widget to `permitted: False` instead of raising and blanking the page.

`frappe.get_all` and `frappe.qb` are deliberately NOT used: both bypass permissions.

RESPONSE CONTRACT
-----------------
Shapes are lifted field-for-field from the mock objects in the compiled frontend
bundle (`vnp_hr_solutions_frontend/.../assets/index-BOZz8Ozn.js`); the mock is quoted
above each builder. Presentation-only keys the bundle supplies as JS component refs or
Tailwind classes (`icon`, `iconBg`, `iconColor`, `color`, `dotColor`) are not emitted --
they are not serialisable and belong to the UI.

Frappe wraps whitelisted returns under "message"; clients read `response.message`.

ENDPOINTS
---------
One bulk call for page load, plus one call per independently-refreshing UI control
(calendar month navigation, "View all" on requests, the three announcement sub-tabs).
No endpoint exists without a UI trigger; all of them share the builders below, so
there is no duplicated query logic.
"""

import datetime
import functools

import frappe
from frappe import _
from frappe.utils import (
	cint,
	cstr,
	flt,
	formatdate,
	get_last_day,
	getdate,
	nowdate,
	strip_html,
)

# HRMS canonical leave maths -- reused so this API can never disagree with the
# Leave Application form about a balance.
from hrms.hr.doctype.leave_application.leave_application import get_leave_details

# ERPNext holiday-list resolution (Employee.holiday_list -> Company.default_holiday_list).
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee

CACHE_PREFIX = "vnp_dash:my"
CACHE_TTL = 300  # seconds; attendance and claims change intra-day

# Calendar `type` values the component accepts:
#   announcement, task, leave, wfh, halfday, holiday, present, notmarked, absent

# The UI's PendingRequestItem branches only on == "Approved"; anything else renders
# as the amber pending pill.
STATUS_PENDING = "Pending"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"

# Expense Claim states meaning "claimed but not settled".
# Derived from ExpenseClaim.set_status() in hrms 15.49.2 -- not invented.
PENDING_CLAIM_STATES = ("Draft", "Submitted", "Unpaid")


# ---------------------------------------------------------------------------
# infrastructure
# ---------------------------------------------------------------------------


def _ok(data, meta):
	return {"status": "success", "data": data, "meta": meta}


def _can(doctype):
	return bool(frappe.has_permission(doctype, "read"))


def _cached(widget, employee, extra, builder):
	"""Read-through cache. A cache backend failure must never fail the request."""
	key = f"{CACHE_PREFIX}:{widget}:{employee or 'noemp'}:{extra}"
	try:
		hit = frappe.cache().get_value(key)
		if hit is not None:
			return hit
	except Exception:
		pass

	value = builder()

	try:
		frappe.cache().set_value(key, value, expires_in_sec=CACHE_TTL)
	except Exception:
		pass
	return value


def clear_my_dashboard_cache(employee=None):
	"""Invalidation entry point used by dashboard/api/cache_hooks.py."""
	try:
		frappe.cache().delete_keys(f"{CACHE_PREFIX}:*{employee or ''}*")
	except Exception:
		pass


def _safe(endpoint, fn):
	"""Log server-side, return a generic client message. Never leaks SQL or a stack."""
	try:
		return fn()
	except (frappe.PermissionError, frappe.ValidationError):
		raise
	except Exception:
		frappe.log_error(
			title=f"{endpoint} failed",
			message=frappe.get_traceback(with_context=True),
		)
		frappe.throw(
			_("Unable to load dashboard data right now. Please retry shortly."),
			title=_("Dashboard unavailable"),
		)


def lazy(fn):
	"""Per-instance memoised property, computed at most once per request.

	Replaces the `def build(): ... return self._lazy("key", build)` boilerplate; the memo
	key is the method name, so it can no longer drift from the property it caches.
	Requires the owning class to expose a `_memo` dict.
	"""

	@property
	@functools.wraps(fn)
	def prop(self):
		key = fn.__name__
		if key not in self._memo:
			self._memo[key] = fn(self)
		return self._memo[key]

	return prop


def endpoint(fn):
	"""`@frappe.whitelist(methods=["GET"])` plus the `_safe` error boundary.

	`functools.wraps` sets `__wrapped__`, so `inspect.signature()` -- which frappe.call
	uses to filter form_dict down to declared parameters -- still sees the real argument
	list rather than (*args, **kwargs). Without that, `cmd` would be forwarded and every
	call would raise TypeError.
	"""

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		return _safe(fn.__name__, lambda: fn(*args, **kwargs))

	return frappe.whitelist(methods=["GET"])(wrapper)


def _req_memo():
	"""Request-scoped memo. `frappe.local` is reset per request, so nothing leaks
	between requests or between sites."""
	store = getattr(frappe.local, "_vnp_dash_memo", None)
	if store is None:
		store = {}
		frappe.local._vnp_dash_memo = store
	return store


def _currency_symbol(currency):
	"""Currency symbol, resolved once per currency per request.

	Previously `_fmt_money` hit the Currency doctype on EVERY call, which meant one
	query per row in the pending-requests list (N+1).
	"""
	if not currency:
		return ""
	memo = _req_memo().setdefault("currency_symbol", {})
	if currency not in memo:
		memo[currency] = cstr(frappe.db.get_value("Currency", currency, "symbol") or "")
	return memo[currency]


def _fmt_money(amount, currency):
	"""Match the mock's literal formatting: "₹45,200". Not fmt_money(), which yields
	"₹ 45,200.00" and would not match the contract. Format is unchanged; only the
	symbol lookup is now memoised."""
	return f"{_currency_symbol(currency)}{flt(amount):,.0f}"


def _num(v):
	"""Emit 6 not 6.0, but keep 6.5."""
	v = flt(v, 2)
	return cint(v) if v == cint(v) else v


def _company_currency(company):
	"""Company default currency, resolved once per company per request."""
	if not company:
		return None
	memo = _req_memo().setdefault("company_currency", {})
	if company not in memo:
		memo[company] = frappe.db.get_value("Company", company, "default_currency")
	return memo[company]


def _daterange_label(from_date, to_date):
	if not from_date:
		return None
	if not to_date or getdate(from_date) == getdate(to_date):
		return formatdate(from_date, "MMM d")
	return f"{formatdate(from_date, 'MMM d')} – {formatdate(to_date, 'MMM d')}"


# ---------------------------------------------------------------------------
# subject + window
# ---------------------------------------------------------------------------


def _resolve_employee():
	"""Resolve the API-key user to their Employee via get_list (permission-applied).

	Returns (employee | None, warnings). Never falls back to "some employee" for users
	with no link (Administrator, integration users).
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)

	if not _can("Employee"):
		return None, [f"User '{user}' has no read permission on Employee."]

	rows = frappe.get_list(
		"Employee",
		filters={"user_id": user},
		fields=[
			"name",
			"employee_name",
			"company",
			"status",
			"date_of_joining",
			"holiday_list",
		],
		# v15 rejects expressions in order_by, so Active-first is applied in Python.
		order_by="creation asc",
		limit_page_length=5,
	)
	rows.sort(key=lambda r: 0 if r.status == "Active" else 1)

	if not rows:
		return None, [
			f"No Employee record is readable for user '{user}' (Employee.user_id). "
			"Returning the empty self-service contract."
		]

	warnings = []
	if len(rows) > 1:
		warnings.append(
			f"More than one Employee is linked to user '{user}'; using '{rows[0].name}'. "
			"Employee.user_id should be unique."
		)

	emp = rows[0]
	if emp.status != "Active":
		warnings.append(f"Employee '{emp.name}' has status '{emp.status}', not 'Active'.")
	return emp, warnings


def _month_window(month=None, year=None):
	"""Default = current month. The UI exposes no date-range control."""
	today_ = getdate(nowdate())
	month = cint(month) or today_.month
	year = cint(year) or today_.year

	if not 1 <= month <= 12:
		frappe.throw(_("`month` must be between 1 and 12."))
	if not 1970 <= year <= 2200:
		frappe.throw(_("`year` is out of range."))

	start = datetime.date(year, month, 1)
	return start, getdate(get_last_day(start)), month, year


# --- attendance window policy -------------------------------------------------
# POLICY: "month_to_date".
#
# For the CURRENT month the window is capped at today; for any past or future month it
# is the whole month. The cap is applied to the NUMERATOR and the DENOMINATOR together --
# present/absent/on_leave/not_marked AND working_days/weekends/holidays/days_elapsed are
# all derived from the same capped window.
#
# Why: previously present-days were month-to-date while working_days was whole-month, so
# on the 6th of a 31-day month a fully-present employee read "4 / 27 days" and the card
# implied 23 absences that had not happened yet.
#
# Pass full_month=1 to any endpoint to force the whole-month window instead.
WINDOW_MONTH_TO_DATE = "month_to_date"
WINDOW_FULL_MONTH = "full_month"


def _window_end(start, end, full_month=False):
	"""(window_end, policy). Caps a current-month window at today."""
	if cint(full_month):
		return end, WINDOW_FULL_MONTH
	today_ = getdate(nowdate())
	if start <= today_ <= end:
		return today_, WINDOW_MONTH_TO_DATE
	return end, WINDOW_FULL_MONTH


def _classify_days(ctx):
	"""Assign EXACTLY ONE classification to every day in the capped window.

	Single source of truth for both the "Present this month" KPI and the calendar, so
	the two can never disagree.

	Precedence: public holiday > weekend > Attendance > approved Leave Application >
	not-marked (past, on/after joining) > upcoming (future) > excluded (before joining).

	Weekend/holiday are CALENDAR facts and are decided first, so a Saturday missing from
	the Holiday List can never be reported as "notmarked" and is always deducted from
	working_days. Attendance on such a day is not lost: the calendar still emits an entry
	so it renders, and `worked_on_weekend` / `worked_on_holiday` count it -- those are
	additive details and do NOT participate in the days_elapsed sum, which stays exact
	because every day lands in exactly one bucket.
	"""
	att_by_date = {getdate(r.attendance_date): r for r in (ctx.att_rows or [])}

	leave_by_date = {}
	for lv in ctx.approved_leaves:
		d, last = max(getdate(lv.from_date), ctx.start), min(getdate(lv.to_date), ctx.win_end)
		while d <= last:
			leave_by_date.setdefault(d, lv)
			d += datetime.timedelta(days=1)

	holiday_by_date = {getdate(h.holiday_date): h for h in ctx.holidays}
	weekend_dates = ctx.weekend_dates
	public_dates = ctx.public_holiday_dates
	lwp = ctx.lwp_types
	today_ = getdate(nowdate())
	doj = getdate(ctx.employee.date_of_joining) if ctx.employee.get("date_of_joining") else None

	counts = dict(
		present=0,
		wfh=0,
		absent=0,
		on_leave=0,
		half_day=0,
		paid_leave=0,
		unpaid_leave=0,
		holiday=0,
		weekend=0,
		notmarked=0,
		upcoming=0,
		excluded=0,
		worked_on_weekend=0,
		worked_on_holiday=0,
	)
	by_date = {}

	cur = ctx.start
	while cur <= ctx.win_end:
		att, lv = att_by_date.get(cur), leave_by_date.get(cur)

		if cur in public_dates:
			hol = holiday_by_date.get(cur)
			label = strip_html(cstr(getattr(hol, "description", "") or "")).strip() or "Holiday"
			by_date[cur] = ("holiday", label, hol)
			counts["holiday"] += 1
			if att and att.status in ("Present", "Work From Home"):
				counts["worked_on_holiday"] += 1

		elif cur in weekend_dates:
			# Counted in stats. Gets a calendar entry ONLY if it was actually worked,
			# so an ordinary weekend stays an empty cell (as in the mock) while a worked
			# weekend remains visible.
			counts["weekend"] += 1
			if att and att.status in ("Present", "Work From Home"):
				counts["worked_on_weekend"] += 1
				etype = "wfh" if att.status == "Work From Home" else "present"
				title = "WFH Tracker" if etype == "wfh" else "Present"
				by_date[cur] = (etype, title, att, True)
			else:
				by_date[cur] = ("weekend", None, None)

		elif att:
			status = att.status
			if status == "Present":
				by_date[cur] = ("present", "Present", att)
				counts["present"] += 1
			elif status == "Work From Home":
				by_date[cur] = ("wfh", "WFH Tracker", att)
				counts["wfh"] += 1
			elif status == "Absent":
				by_date[cur] = ("absent", "Absent", att)
				counts["absent"] += 1
			elif status == "Half Day":
				title = f"Half Day ({att.leave_type})" if att.leave_type else "Half Day"
				by_date[cur] = ("halfday", title, att)
				counts["half_day"] += 1
			elif status == "On Leave":
				by_date[cur] = ("leave", att.leave_type or "On Leave", att)
				counts["on_leave"] += 1
				if att.leave_type and att.leave_type in lwp:
					counts["unpaid_leave"] += 1
				else:
					counts["paid_leave"] += 1
			else:
				by_date[cur] = ("present", status, att)
				counts["present"] += 1

		elif lv:
			by_date[cur] = ("leave", lv.leave_type or "On Leave", lv)
			counts["on_leave"] += 1
			if lv.leave_type and lv.leave_type in lwp:
				counts["unpaid_leave"] += 1
			else:
				counts["paid_leave"] += 1

		elif cur <= today_ and (doj is None or cur >= doj):
			by_date[cur] = ("notmarked", "Not marked", None)
			counts["notmarked"] += 1

		elif doj is not None and cur < doj:
			by_date[cur] = ("excluded", None, None)
			counts["excluded"] += 1

		else:
			by_date[cur] = ("upcoming", None, None)
			counts["upcoming"] += 1

		cur += datetime.timedelta(days=1)

	days_elapsed = max((ctx.win_end - ctx.start).days + 1, 0)
	# A worked weekend/holiday is NOT deducted -- it was worked. Pre-joining days are
	# deducted; future days inside a full_month window are not (they are working days
	# that simply have not happened yet).
	working_days = max(
		days_elapsed - counts["weekend"] - counts["holiday"] - counts["excluded"], 0
	)

	return {
		"by_date": by_date,
		"counts": counts,
		"days_elapsed": days_elapsed,
		"working_days": working_days,
		# worked weekends/holidays are NOT added here -- those days are weekend/holiday
		# buckets and are already excluded from working_days.
		"present_days": counts["present"] + counts["wfh"] + (0.5 * counts["half_day"]),
	}


# Weekday indices treated as the standard weekend when the Holiday List does not fully
# describe one. ERPNext/HRMS v15 has NO multi-day weekend config: the only setting is
# `Holiday List.weekly_off`, a SINGLE Select. So Sat+Sun is an assumption, kept here as
# one editable constant. Set to {6} for a six-day working week (Sunday off only).
STANDARD_WEEKEND_WEEKDAYS = {5, 6}  # Mon=0 ... Sat=5, Sun=6

WEEKEND_FROM_LIST = "holiday_list"
WEEKEND_FROM_WEEKDAY = "weekday_fallback"
WEEKEND_RECONCILED = "reconciled"


def _weekend_info(holiday_rows, holiday_list, start, end):
	"""(weekend_dates, source, detail).

	Weekends were previously taken from Holiday List `weekly_off` rows and trusted as
	complete. They are not: this bench's lists mark only ONE of the two weekend days
	(weekly_off = "Sunday"), so every Saturday was treated as a working day and rendered
	"notmarked" -- July 2026 reported 4 weekends / 27 working days instead of 8 / 23.

	The list is now cross-checked rather than trusted:
	  * collect the distinct weekday(s) actually flagged weekly_off in the window;
	  * if that set is EMPTY            -> weekday_fallback (use STANDARD_WEEKEND_WEEKDAYS)
	  * if it MISSES a standard weekend day -> reconciled (union with the standard set)
	  * if it covers the standard set   -> holiday_list (trusted as-is)

	Explicitly-flagged dates are always kept, even on a non-standard weekday, so a
	company with (say) a Tuesday weekly off keeps it.
	"""
	flagged = {getdate(h.holiday_date) for h in holiday_rows if cint(h.weekly_off)}
	flagged_weekdays = {d.weekday() for d in flagged}

	weekday_names = (
		"Monday",
		"Tuesday",
		"Wednesday",
		"Thursday",
		"Friday",
		"Saturday",
		"Sunday",
	)
	# Holiday List.weekly_off is the only explicit config, and it names ONE weekday.
	configured = None
	if holiday_list and _can("Holiday List"):
		configured = cstr(frappe.db.get_value("Holiday List", holiday_list, "weekly_off") or "")
	configured_weekday = (
		weekday_names.index(configured) if configured in weekday_names else None
	)

	known = set(flagged_weekdays)
	if configured_weekday is not None:
		known.add(configured_weekday)

	missing = STANDARD_WEEKEND_WEEKDAYS - known
	if not known:
		wanted, source = set(STANDARD_WEEKEND_WEEKDAYS), WEEKEND_FROM_WEEKDAY
	elif missing:
		wanted, source = known | STANDARD_WEEKEND_WEEKDAYS, WEEKEND_RECONCILED
	else:
		wanted, source = known, WEEKEND_FROM_LIST

	out = set(flagged)  # never drop an explicitly flagged date
	cur = start
	while cur <= end:
		if cur.weekday() in wanted:
			out.add(cur)
		cur += datetime.timedelta(days=1)

	detail = {
		"source": source,
		"weekly_off_weekdays": sorted(weekday_names[i] for i in sorted(known)),
		"supplemented_with": sorted(weekday_names[i] for i in sorted(missing)) if missing else [],
		"flagged_rows": len(flagged),
	}
	return out, source, detail


# ---------------------------------------------------------------------------
# data access
# ---------------------------------------------------------------------------


def _fetch_holidays(holiday_list, start, end):
	"""Holiday rows in window.

	`Holiday` is a child doctype -- querying it directly with get_list raises
	PermissionError, so the parent Holiday List is loaded (permission-checked) and its
	rows are filtered in Python.
	"""
	if not holiday_list or not _can("Holiday List"):
		return []
	try:
		doc = frappe.get_cached_doc("Holiday List", holiday_list)
	except frappe.DoesNotExistError:
		return []
	return [h for h in doc.holidays if h.holiday_date and start <= getdate(h.holiday_date) <= end]


def _fetch_attendance(employee, start, end):
	if not _can("Attendance"):
		return None
	return frappe.get_list(
		"Attendance",
		filters={
			"employee": employee,
			"docstatus": 1,
			"attendance_date": ["between", [start, end]],
		},
		fields=["attendance_date", "status", "leave_type"],
		order_by="attendance_date asc",
		limit_page_length=0,
	)


def _fetch_approved_leaves(employee, start, end):
	if not _can("Leave Application"):
		return []
	return frappe.get_list(
		"Leave Application",
		filters={
			"employee": employee,
			"docstatus": 1,
			"status": "Approved",
			"from_date": ["<=", end],
			"to_date": [">=", start],
		},
		fields=["leave_type", "from_date", "to_date"],
		limit_page_length=0,
	)


def _fetch_leave_details(employee):
	"""HRMS per-leave-type allocation/consumption.

	Bounded by the number of leave types allocated to one employee (single digits),
	and the result is cached.
	"""
	if not (_can("Leave Allocation") and _can("Leave Ledger Entry")):
		return None
	return get_leave_details(employee, nowdate())


def _fetch_self(employee):
	"""The logged-in user's own Employee row, for the celebration widgets.

	Pinned to `name = employee` so birthdays/anniversaries return the logged-in user's
	data only -- it does not depend on how User Permissions happen to be configured.
	get_list still applies permissions on top.

	GAP-9: a self-only birthday/anniversary panel shows at most one card per month. If
	the panel is meant to show colleagues, this filter has to widen to a defined scope
	(company / department / reporting line) -- that is a product decision.
	"""
	if not employee or not _can("Employee"):
		return None
	return frappe.get_list(
		"Employee",
		filters={"name": employee, "status": "Active"},
		fields=["name", "employee_name", "image", "date_of_birth", "date_of_joining"],
		limit_page_length=1,
	)


# ---------------------------------------------------------------------------
# KPI CARDS
# mock `m` @1792814:
#   {label:"Leave balance",      value:"18 days"}
#   {label:"Present this month", value:"8 / 22 days", subtitle:<span>"+20"</span>}
#   {label:"Last salary",        value:"₹45,200", sub:"5 URGENT"}
#   {label:"Pending claims",     value:"₹2,400",  trendUp:false}
# `subtitle` carries a literal "+20" and `sub` a literal "5 URGENT"; neither is
# derivable from any doctype, so both are emitted as null. See GAP-1 / GAP-1b.
# ---------------------------------------------------------------------------


def _kpi_leave_balance(details):
	if details is None:
		return {"label": "Leave balance", "value": "0 days", "days": 0.0, "permitted": False}

	alloc = details.get("leave_allocation") or {}
	days = flt(sum(flt(v.get("remaining_leaves")) for v in alloc.values()), 2)
	return {
		"label": "Leave balance",
		"value": f"{_num(days)} days",
		"days": days,
		"by_type": {k: flt(v.get("remaining_leaves"), 2) for k, v in alloc.items()},
		"permitted": True,
	}


def _kpi_present_this_month(ctx):
	"""Numerator and denominator both derived from ctx's capped window (see POLICY)."""
	if ctx.att_rows is None:
		return {
			"label": "Present this month",
			"value": "0 / 0 days",
			"present_days": 0,
			"working_days": 0,
			"badge": None,
			"permitted": False,
		}

	cls = ctx.day_classification
	present = cls["present_days"]
	working_days = cls["working_days"]

	return {
		"label": "Present this month",
		"value": f"{_num(present)} / {working_days} days",
		"present_days": flt(present, 2),
		"working_days": working_days,
		"badge": None,  # GAP-1: "+20" meaning undefined
		# additive: makes the window explicit to the client
		"window": ctx.window_policy,
		"window_end": ctx.win_end.isoformat(),
		"days_elapsed": cls["days_elapsed"],
		"permitted": True,
	}


def _kpi_last_salary(employee, company):
	currency = _company_currency(company)
	base = {
		"label": "Last salary",
		"value": _fmt_money(0, currency),
		"amount": 0.0,
		"currency": currency,
		"period": None,
		"salary_slip": None,
		"sub": None,  # GAP-1b: "5 URGENT" meaning undefined
	}

	if not employee or not _can("Salary Slip"):
		return {**base, "permitted": False}

	rows = frappe.get_list(
		"Salary Slip",
		filters={"employee": employee, "docstatus": 1},
		fields=["name", "net_pay", "currency", "start_date"],
		order_by="end_date desc, posting_date desc",
		limit_page_length=1,
	)
	if not rows:
		return {**base, "permitted": True}

	slip = rows[0]
	return {
		**base,
		"value": _fmt_money(slip.net_pay, slip.currency),
		"amount": flt(slip.net_pay, 2),
		"currency": slip.currency,
		"period": formatdate(slip.start_date, "MMM yyyy") if slip.start_date else None,
		"salary_slip": slip.name,
		"permitted": True,
	}


def _kpi_pending_claims(employee, company):
	currency = _company_currency(company)
	base = {
		"label": "Pending claims",
		"value": _fmt_money(0, currency),
		"amount": 0.0,
		"count": 0,
		"currency": currency,
		"states": list(PENDING_CLAIM_STATES),
		"trendUp": False,
	}

	if not employee or not _can("Expense Claim"):
		return {**base, "permitted": False}

	# Summed in Python rather than via frappe.qb: qb bypasses permissions, and one
	# employee's open claims are a small set.
	rows = frappe.get_list(
		"Expense Claim",
		filters={
			"employee": employee,
			"docstatus": ["<", 2],
			"status": ["in", PENDING_CLAIM_STATES],
		},
		fields=["total_claimed_amount"],
		limit_page_length=0,
	)
	amount = sum(flt(r.total_claimed_amount) for r in rows)
	return {
		**base,
		"value": _fmt_money(amount, currency),
		"amount": flt(amount, 2),
		"count": len(rows),
		"permitted": True,
	}


def _build_kpis(ctx):
	return [
		_kpi_leave_balance(ctx.details),
		_kpi_present_this_month(ctx),
		_kpi_last_salary(ctx.employee_id, ctx.company),
		_kpi_pending_claims(ctx.employee_id, ctx.company),
	]


# ---------------------------------------------------------------------------
# ATTENDANCE CALENDAR
# mock `data` (mFe @1787018): {id, title, name:"Self", date:Date, type}
# mock `stats` (fFe @1788814): 6 x {label, value, type} in this exact order
# Weekends are counted in `stats` but get no `data` entry -- matching the mock, which
# has 8 weekends in stats and none in data.
# The mock typed its WFH day "present"; the component supports a first-class "wfh",
# so "wfh" is emitted here. See GAP-4.
# ---------------------------------------------------------------------------


def _calendar_stats(payable, present, absent, on_leave, holidays, weekends):
	return [
		{"label": "Payable days", "value": _num(payable), "type": "payable"},
		{"label": "Present", "value": _num(present), "type": "present"},
		{"label": "Absent", "value": _num(absent), "type": "absent"},
		{"label": "On leave", "value": _num(on_leave), "type": "leave"},
		{"label": "Holidays", "value": _num(holidays), "type": "holiday"},
		{"label": "Weekends", "value": _num(weekends), "type": "weekend"},
	]


def _build_calendar(ctx):
	if not ctx.employee or ctx.att_rows is None:
		return {
			"month": ctx.month,
			"year": ctx.year,
			"data": [],
			"stats": _calendar_stats(0, 0, 0, 0, 0, 0),
			"totals": {},
			"permitted": False,
		}

	# Every counter below comes from the ONE shared classification (see _classify_days),
	# so the calendar and the "Present this month" KPI cannot drift apart.
	cls = ctx.day_classification
	c = cls["counts"]

	events = []
	for d in sorted(cls["by_date"]):
		entry = cls["by_date"][d]
		etype, title = entry[0], entry[1]
		if etype in ("weekend", "excluded", "upcoming"):
			continue  # counted in stats, no calendar cell (matches the mock)
		events.append(
			{
				"id": len(events) + 1,
				"title": title,
				"name": "National Holiday" if etype == "holiday" else "Self",
				"date": d.isoformat(),
				"type": etype,
			}
		)

	# GAP-5: the mock's own arithmetic is 19 == present(16) + on_leave(2) + holidays(1),
	# so payable = attended + paid leave + public holidays + half days at 0.5. This
	# excludes weekends and is NOT how Salary Slip.payment_days works. Confirm before
	# using for payroll.
	payable = c["present"] + c["wfh"] + c["paid_leave"] + c["holiday"] + (0.5 * c["half_day"])

	return {
		"month": ctx.month,
		"year": ctx.year,
		"data": events,
		"stats": _calendar_stats(
			payable,
			c["present"] + c["wfh"],
			c["absent"],
			c["on_leave"],
			c["holiday"],
			c["weekend"],
		),
		"totals": {
			"present": c["present"],
			"wfh": c["wfh"],
			"absent": c["absent"],
			"on_leave": c["on_leave"],
			"paid_leave": c["paid_leave"],
			"unpaid_leave": c["unpaid_leave"],
			"half_day": c["half_day"],
			"holidays": c["holiday"],
			"weekends": c["weekend"],
			"not_marked": c["notmarked"],
			"payable_days": flt(payable, 2),
			"days_in_month": (ctx.end - ctx.start).days + 1,
			# additive: the window every counter above was computed on. Classifications
			# are mutually exclusive, so these sum to days_elapsed exactly:
			#   present+wfh+absent+on_leave+half_day+holidays+weekends+not_marked
			#   +upcoming+excluded
			"days_elapsed": cls["days_elapsed"],
			"working_days": cls["working_days"],
			"upcoming_days": c["upcoming"],
			"excluded_days": c["excluded"],
			"window": ctx.window_policy,
			"window_end": ctx.win_end.isoformat(),
			# "holiday_list" | "weekday_fallback" | "reconciled"
			"weekend_source": ctx.weekend_source,
			"weekend_detail": ctx.weekend_detail,
			"worked_on_weekend": c["worked_on_weekend"],
			"worked_on_holiday": c["worked_on_holiday"],
		},
		"permitted": True,
	}


# ---------------------------------------------------------------------------
# LEAVE BALANCE CHART
# mock `f` @1793050: {label, used, total, remaining, color}
# No `is_lwp` flag: HRMS refuses to allocate a leave-without-pay type ("cannot be
# allocated since it is leave without pay") and this chart is built from allocations, so
# it could never be true. Unpaid leave surfaces in the calendar totals instead.
# ---------------------------------------------------------------------------


def _build_leave_chart(ctx):
	if ctx.details is None:
		return {"data": [], "permitted": False}

	alloc = ctx.details.get("leave_allocation") or {}
	return {
		"data": [
			{
				"label": lt,
				"used": flt(v.get("leaves_taken"), 2),
				"total": flt(v.get("total_leaves"), 2),
				"remaining": flt(v.get("remaining_leaves"), 2),
				"pending_approval": flt(v.get("leaves_pending_approval"), 2),
				"expired": flt(v.get("expired_leaves"), 2),
			}
			for lt, v in sorted(alloc.items())
		],
		"permitted": True,
	}


# ---------------------------------------------------------------------------
# PENDING REQUESTS
# mock `h` @1793330: {title, subtitle, status} -- only 3 keys.
# CONTRACT HAZARD: the UI picks each icon by substring-matching `title`
# ("leave" / "expense"|"claim" / "attendance"|"clock"), so these titles keep those
# keywords deliberately. `type`/`date_or_range`/`name` are additive.
# ---------------------------------------------------------------------------


def _build_pending_requests(ctx, limit=10, only_open=0):
	"""RECONCILIATION (chose option b -- documented, not silently "fixed").

	This list is deliberately RECENT REQUEST ACTIVITY across four doctypes, not an
	open-items queue: the mock itself ships an already-"Approved" WFH row, so filtering
	to open-only would contradict the design.

	The "Pending claims" KPI is a different, narrower thing: the SUM of Expense Claim
	amounts in Draft/Submitted/Unpaid. So the two are reconcilable rather than equal:

	    kpi.pending_claims.count  ==  len([r for r in data
	                                       if r["type"] == "Expense Claim" and r["is_open"]])

	Every row now carries `is_open`, and the payload carries `open_count` /
	`open_count_by_type`, so a client can prove that identity. Pass only_open=1 to make
	the list itself an open-items queue.
	"""
	if not ctx.employee_id:
		return {"data": [], "total": 0, "open_count": 0, "permitted": False}

	limit = max(1, min(cint(limit) or 10, 50))
	only_open = cint(only_open)
	fetch = limit * 3  # over-fetch per source, then merge and slice
	emp = ctx.employee_id
	items = []

	if _can("Leave Application"):
		for r in frappe.get_list(
			"Leave Application",
			filters={"employee": emp, "docstatus": ["<", 2], "status": ["!=", "Cancelled"]},
			fields=["name", "leave_type", "from_date", "to_date", "status", "modified"],
			order_by="modified desc",
			limit_page_length=fetch,
		):
			rng = _daterange_label(r.from_date, r.to_date)
			items.append(
				{
					"title": r.leave_type or "Leave",  # keeps the "leave" icon keyword
					"subtitle": rng,
					"status": {
						"Open": STATUS_PENDING,
						"Approved": STATUS_APPROVED,
						"Rejected": STATUS_REJECTED,
					}.get(r.status, STATUS_PENDING),
					"type": "Leave Application",
					"name": r.name,
					"date_or_range": rng,
					"is_open": r.status == "Open",
					"_sort": r.modified,
				}
			)

	if _can("Expense Claim"):
		currency = _company_currency(ctx.company)
		for r in frappe.get_list(
			"Expense Claim",
			filters={"employee": emp, "docstatus": ["<", 2]},
			fields=[
				"name",
				"total_claimed_amount",
				"status",
				"approval_status",
				"posting_date",
				"modified",
			],
			order_by="modified desc",
			limit_page_length=fetch,
		):
			if r.status == "Rejected":
				status = STATUS_REJECTED
			elif r.approval_status == "Approved":
				status = STATUS_APPROVED
			else:
				status = STATUS_PENDING
			items.append(
				{
					"title": "Expense Claim",  # keeps the "claim" icon keyword
					"subtitle": _fmt_money(r.total_claimed_amount, currency),
					"status": status,
					"type": "Expense Claim",
					"name": r.name,
					# was null; the claim's posting date is the only date it carries
					"date_or_range": formatdate(r.posting_date, "MMM d") if r.posting_date else None,
					"amount": flt(r.total_claimed_amount, 2),
					# matches the "Pending claims" KPI states exactly
					"is_open": r.status in PENDING_CLAIM_STATES,
					"_sort": r.modified,
				}
			)

	if _can("Attendance Request"):
		# Attendance Request has no status field -- state is docstatus. `reason` is a
		# Select of exactly "Work From Home|On Duty" (verified in hrms 15.49.2).
		for r in frappe.get_list(
			"Attendance Request",
			filters={"employee": emp, "docstatus": ["<", 2]},
			fields=["name", "from_date", "to_date", "reason", "docstatus", "modified"],
			order_by="modified desc",
			limit_page_length=fetch,
		):
			is_wfh = r.reason == "Work From Home"
			rng = _daterange_label(r.from_date, r.to_date)
			items.append(
				{
					# GAP-11: "Regularize" is not an HRMS term -- confirm this wording.
					"title": "WFH Request" if is_wfh else "Attendance",
					"subtitle": rng if is_wfh else (f"{rng} · Regularize" if rng else "Regularize"),
					"status": STATUS_APPROVED if cint(r.docstatus) == 1 else STATUS_PENDING,
					"type": "Attendance Request",
					"name": r.name,
					"date_or_range": rng,
					"reason": r.reason,
					"is_open": cint(r.docstatus) == 0,
					"_sort": r.modified,
				}
			)

	items.sort(key=lambda x: x.get("_sort") or "", reverse=True)
	for it in items:
		it.pop("_sort", None)

	if only_open:
		items = [it for it in items if it.get("is_open")]

	open_by_type = {}
	for it in items:
		if it.get("is_open"):
			open_by_type[it["type"]] = open_by_type.get(it["type"], 0) + 1

	return {
		"data": items[:limit],
		"total": len(items),
		# additive: lets a client reconcile this list against the Pending claims KPI
		"open_count": sum(open_by_type.values()),
		"open_count_by_type": open_by_type,
		"scope": "open_only" if only_open else "recent_activity",
		"permitted": True,
	}


# ---------------------------------------------------------------------------
# ANNOUNCEMENTS / BIRTHDAYS / ANNIVERSARIES  (3 sub-tabs)
# mock `g` @1793520 (announcements) / `b` @1796000 (holidays):
#   {title, date:"Apr 8, 2026", description, link[, src]}
# mock `x` @1795300 (birthdays):     {title:"X's Birthday", date, src, description, link}
# mock `E` @1796900 (anniversaries): {title:"X's Work Anniversary (2 Years)", ...}
#
# The "Announcements" sub-tab renders [...g, ...b] -- announcements CONCATENATED WITH
# holidays. TODO(GAP-3): no Announcement doctype exists in this bench (verified across
# erpnext, hrms, india_compliance, lms and all vnp_* apps), so `announcements` is always
# empty. The holiday half is real.
#
# GAP-8: the mock's celebratory prose ("cake cutting ceremony at 4:00 PM") has no data
# source; a deterministic sentence is generated instead.
# ---------------------------------------------------------------------------


def _build_announcements(ctx, limit=10):
	limit = max(1, min(cint(limit) or 10, 50))
	holidays = []

	if ctx.holiday_list and _can("Holiday List"):
		today_ = getdate(nowdate())
		try:
			doc = frappe.get_cached_doc("Holiday List", ctx.holiday_list)
			upcoming = sorted(
				(
					h
					for h in doc.holidays
					if h.holiday_date
					and not cint(h.weekly_off)
					and getdate(h.holiday_date) >= today_
				),
				key=lambda h: getdate(h.holiday_date),
			)[:limit]
		except frappe.DoesNotExistError:
			upcoming = []

		for h in upcoming:
			label = strip_html(cstr(h.description or "")).strip() or "Holiday"
			holidays.append(
				{
					"title": label,
					"date": formatdate(h.holiday_date, "MMM d, yyyy"),
					"description": label,
					"link": "#",
					"src": None,
					"type": "holiday",
					"raw_date": cstr(h.holiday_date),
				}
			)

	return {
		"announcements": [],  # GAP-3: no Announcement doctype
		"holidays": holidays,
		"items": holidays[:limit],  # what the sub-tab renders: [...g, ...b]
		"is_stub": True,
		"gap": "No Announcement doctype exists in this bench. Only the holiday half of "
		"this sub-tab is real.",
		"permitted": True,
	}


def _build_birthdays(ctx, limit=25):
	rows = ctx.self_rows
	if rows is None:
		return {"data": [], "total": 0, "permitted": False}

	limit = max(1, min(cint(limit) or 25, 100))
	out = []
	for r in rows:
		if not r.date_of_birth or getdate(r.date_of_birth).month != ctx.month:
			continue
		try:
			shown = datetime.date(ctx.year, ctx.month, getdate(r.date_of_birth).day)
		except ValueError:
			continue  # 29 Feb in a non-leap year
		out.append(
			{
				"title": f"{r.employee_name}'s Birthday",
				"date": formatdate(shown, "MMM d, yyyy"),
				"description": f"Wishing {r.employee_name} a very Happy Birthday!",
				"link": "#",
				"src": r.image or None,
				"type": "birthday",
				"employee": r.name,
				"employee_name": r.employee_name,
				"raw_date": shown.isoformat(),
			}
		)

	out.sort(key=lambda x: x["raw_date"])
	return {"data": out[:limit], "total": len(out), "permitted": True}


def _build_anniversaries(ctx, limit=25):
	rows = ctx.self_rows
	if rows is None:
		return {"data": [], "total": 0, "permitted": False}

	limit = max(1, min(cint(limit) or 25, 100))
	out = []
	for r in rows:
		if not r.date_of_joining:
			continue
		doj = getdate(r.date_of_joining)
		years = ctx.year - doj.year
		if doj.month != ctx.month or years <= 0:
			continue
		try:
			shown = datetime.date(ctx.year, ctx.month, doj.day)
		except ValueError:
			continue
		out.append(
			{
				"title": f"{r.employee_name}'s Work Anniversary "
				f"({'1 Year' if years == 1 else f'{years} Years'})",
				"date": formatdate(shown, "MMM d, yyyy"),
				"description": f"Happy work anniversary to {r.employee_name}!",
				"link": "#",
				"src": r.image or None,
				"type": "anniversary",
				"employee": r.name,
				"employee_name": r.employee_name,
				"years": years,
				"raw_date": shown.isoformat(),
			}
		)

	out.sort(key=lambda x: x["raw_date"])
	return {"data": out[:limit], "total": len(out), "permitted": True}


# ---------------------------------------------------------------------------
# request context -- lazy so a cache hit never touches the database
# ---------------------------------------------------------------------------


class _Ctx:
	__slots__ = (
		"employee",
		"employee_id",
		"company",
		"warnings",
		"start",
		"end",
		"win_end",
		"window_policy",
		"month",
		"year",
		"_memo",
	)

	def __init__(self, month=None, year=None, full_month=False):
		employee, warnings = _resolve_employee()
		self.employee = employee
		self.employee_id = employee.name if employee else None
		self.company = employee.company if employee else None
		self.warnings = warnings
		self.start, self.end, self.month, self.year = _month_window(month, year)
		# One window policy, applied to every numerator AND denominator downstream.
		self.win_end, self.window_policy = _window_end(self.start, self.end, full_month)
		self._memo = {}

	@property
	def month_key(self):
		# window policy is part of the key: month_to_date and full_month payloads differ
		return f"{self.year}-{self.month:02d}:{self.window_policy}"

	@lazy
	def holiday_list(self):
		if not self.employee:
			return None
		hl = self.employee.get("holiday_list") or get_holiday_list_for_employee(
			self.employee.name, raise_exception=False
		)
		if not hl:
			self.warnings.append(
				f"No Holiday List resolved for '{self.employee.name}'. "
				"Weekend and holiday counts will be 0."
			)
		return hl

	@lazy
	def holidays(self):
		# Capped window, so weekend/holiday counters can never span days the attendance
		# counters do not.
		if not self.employee:
			return []
		return _fetch_holidays(self.holiday_list, self.start, self.win_end)

	@lazy
	def _weekends(self):
		if not self.employee:
			return set(), WEEKEND_FROM_LIST, {}
		dates, source, detail = _weekend_info(
			self.holidays, self.holiday_list, self.start, self.win_end
		)
		hl = self.holiday_list or "(none)"
		if source == WEEKEND_FROM_WEEKDAY:
			self.warnings.append(
				f"Holiday List '{hl}' has no weekly_off rows in {self.year}-{self.month:02d}; "
				f"weekends were derived from the calendar ({', '.join(detail['supplemented_with']) or 'Sat/Sun'}). "
				"Generate weekly offs on the Holiday List to make this exact."
			)
		elif source == WEEKEND_RECONCILED:
			self.warnings.append(
				f"Holiday List '{hl}' marks weekly offs only on "
				f"{', '.join(detail['weekly_off_weekdays']) or '(none)'}; "
				f"added {', '.join(detail['supplemented_with'])} from the calendar so those days "
				"are not counted as working days. If this company genuinely works them, set "
				"STANDARD_WEEKEND_WEEKDAYS accordingly."
			)
		return dates, source, detail

	@property
	def weekend_dates(self):
		return self._weekends[0]

	@property
	def weekend_source(self):
		return self._weekends[1]

	@property
	def weekend_detail(self):
		return self._weekends[2]

	@lazy
	def public_holiday_dates(self):
		return {getdate(h.holiday_date) for h in self.holidays if not cint(h.weekly_off)}

	@lazy
	def day_classification(self):
		"""One mutually-exclusive classification per day, shared by the KPI and calendar."""
		return _classify_days(self)

	@lazy
	def att_rows(self):
		if not self.employee:
			return None
		rows = _fetch_attendance(self.employee.name, self.start, self.win_end)
		if rows is None:
			self.warnings.append("No read permission on Attendance.")
		return rows

	@lazy
	def approved_leaves(self):
		if not self.employee:
			return []
		return _fetch_approved_leaves(self.employee.name, self.start, self.win_end)

	@lazy
	def details(self):
		if not self.employee:
			return None
		return _cached(
			"leave_details",
			self.employee.name,
			nowdate(),
			lambda: _fetch_leave_details(self.employee.name),
		)

	@property
	def lwp_types(self):
		return set((self.details or {}).get("lwps") or [])

	@lazy
	def self_rows(self):
		return _fetch_self(self.employee_id)

	def meta(self, **extra):
		meta = {
			"employee": self.employee_id,
			"employee_name": self.employee.employee_name if self.employee else None,
			"employee_linked": bool(self.employee),
			"company": self.company,
			"month": self.month,
			"year": self.year,
			"from_date": self.start.isoformat(),
			"to_date": self.end.isoformat(),
			# Attendance-derived widgets use this capped window for numerator AND
			# denominator alike. "month_to_date" for the current month unless
			# full_month=1 was passed.
			"attendance_window": {
				"policy": self.window_policy,
				"from_date": self.start.isoformat(),
				"to_date": self.win_end.isoformat(),
				"days": max((self.win_end - self.start).days + 1, 0),
			},
			"generated_on": frappe.utils.now(),
			"cache_ttl": CACHE_TTL,
		}
		if self.warnings:
			meta["warnings"] = self.warnings
		meta.update(extra)
		return meta


def _w(widget, ctx, extra, builder):
	"""Per-widget cache, keyed by widget + employee + month."""
	return _cached(widget, ctx.employee_id, extra, builder)


# ---------------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------------


@endpoint
def get_my_dashboard(
	month=None, year=None, requests_limit=10, announcements_limit=10, full_month=0
):
	"""Page load: every widget on the My Dashboard tab in one round trip."""
	ctx = _Ctx(month, year, full_month)
	mk = ctx.month_key
	return _ok(
		{
			"kpis": _w("kpis", ctx, mk, lambda: _build_kpis(ctx)),
			"attendance_calendar": _w("calendar", ctx, mk, lambda: _build_calendar(ctx)),
			"leave_balance_chart": _w(
				"leave_chart", ctx, nowdate(), lambda: _build_leave_chart(ctx)
			),
			"pending_requests": _w(
				"pending_requests",
				ctx,
				f"{nowdate()}:{cint(requests_limit)}",
				lambda: _build_pending_requests(ctx, requests_limit),
			),
			"announcements": _w(
				"announcements",
				ctx,
				f"{nowdate()}:{cint(announcements_limit)}",
				lambda: _build_announcements(ctx, announcements_limit),
			),
			"birthdays": _w("birthdays", ctx, mk, lambda: _build_birthdays(ctx)),
			"anniversaries": _w("anniversaries", ctx, mk, lambda: _build_anniversaries(ctx)),
		},
		ctx.meta(tab="My Dashboard"),
	)


@endpoint
def get_attendance_calendar(month=None, year=None, full_month=0):
	"""Calendar month navigation."""
	ctx = _Ctx(month, year, full_month)
	return _ok(_w("calendar", ctx, ctx.month_key, lambda: _build_calendar(ctx)), ctx.meta())


@endpoint
def get_pending_requests(limit=10, only_open=0):
	""""View all" on the Pending Requests list."""
	ctx = _Ctx()
	return _ok(
		_w(
			"pending_requests",
			ctx,
			f"{nowdate()}:{cint(limit)}:{cint(only_open)}",
			lambda: _build_pending_requests(ctx, limit, only_open),
		),
		ctx.meta(),
	)


@endpoint
def get_announcements(limit=10):
	"""Announcements sub-tab. Stub for announcements; real for holidays (GAP-3)."""
	ctx = _Ctx()
	return _ok(
		_w(
			"announcements",
			ctx,
			f"{nowdate()}:{cint(limit)}",
			lambda: _build_announcements(ctx, limit),
		),
		ctx.meta(),
	)


@endpoint
def get_birthdays(month=None, year=None, limit=25):
	"""Birthdays sub-tab."""
	ctx = _Ctx(month, year)
	return _ok(
		_w(
			"birthdays",
			ctx,
			f"{ctx.month_key}:{cint(limit)}",
			lambda: _build_birthdays(ctx, limit),
		),
		ctx.meta(),
	)


@endpoint
def get_anniversaries(month=None, year=None, limit=25):
	"""Anniversaries sub-tab."""
	ctx = _Ctx(month, year)
	return _ok(
		_w(
			"anniversaries",
			ctx,
			f"{ctx.month_key}:{cint(limit)}",
			lambda: _build_anniversaries(ctx, limit),
		),
		ctx.meta(),
	)
