app_name = "vnp_hr_solutions_dashboard"
app_title = "VNP HR Solutions Dashboard"
app_publisher = "Flitz Interactive"
app_description = "VNP HR Solutions Dashboard"
app_email = "info@flitzinteractive.com"
app_license = "mit"


# My Dashboard read-cache invalidation.
# The dashboard read API caches per (widget, employee, month) with a 300s TTL. Without
# these hooks an employee who applies for leave or files a claim keeps seeing stale
# dashboard/api/cache_hooks.py.
# NOTE: whitelisted endpoints need NO hooks to be reachable; these are purely for
# cache correctness. Everything else in this app works with hooks.py untouched.
_INVALIDATE = "vnp_hr_solutions_dashboard.dashboard.api.cache_hooks.invalidate_for_employee"
_INVALIDATE_ALL = "vnp_hr_solutions_dashboard.dashboard.api.cache_hooks.invalidate_all"

doc_events = {
	# employee-scoped writes -> targeted invalidation
	"Attendance": {
		"on_update": _INVALIDATE,
		"on_submit": _INVALIDATE,
		"on_cancel": _INVALIDATE,
		"on_trash": _INVALIDATE,
	},
	"Leave Application": {
		"on_update": _INVALIDATE,
		"on_submit": _INVALIDATE,
		"on_cancel": _INVALIDATE,
		"on_trash": _INVALIDATE,
	},
	"Leave Allocation": {
		"on_submit": _INVALIDATE,
		"on_cancel": _INVALIDATE,
	},
	"Leave Ledger Entry": {
		"on_submit": _INVALIDATE,
		"on_cancel": _INVALIDATE,
	},
	"Expense Claim": {
		"on_update": _INVALIDATE,
		"on_submit": _INVALIDATE,
		"on_cancel": _INVALIDATE,
		"on_trash": _INVALIDATE,
	},
	"Attendance Request": {
		"on_update": _INVALIDATE,
		"on_submit": _INVALIDATE,
		"on_cancel": _INVALIDATE,
		"on_trash": _INVALIDATE,
	},
	# Team Dashboard reads this for the comp-off approval category.
	"Compensatory Leave Request": {
		"on_update": _INVALIDATE,
		"on_submit": _INVALIDATE,
		"on_cancel": _INVALIDATE,
		"on_trash": _INVALIDATE,
	},
	"Salary Slip": {
		"on_submit": _INVALIDATE,
		"on_cancel": _INVALIDATE,
	},
	# org-wide masters feeding birthdays / anniversaries / holidays -> broad flush
	"Holiday List": {
		"on_update": _INVALIDATE_ALL,
		"on_trash": _INVALIDATE_ALL,
	},
	"Employee": {
		"on_update": _INVALIDATE_ALL,
		"on_trash": _INVALIDATE_ALL,
	},
}
