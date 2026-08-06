# Copyright (c) 2026, Flitz Interactive and contributors
# For license information, please see license.txt
"""Write-through invalidation for the My Dashboard read cache.

The read cache (5 min TTL) is otherwise visibly stale: an employee who applies for
leave would keep seeing the old Leave balance / Pending requests for up to 5 minutes.
These handlers drop that employee's cached widgets on any relevant write.

Wired via `doc_events` in hooks.py. Keep them cheap and non-throwing -- a cache
failure must never block a document submit.
"""

import frappe

from vnp_hr_solutions_dashboard.dashboard.api.my_dashboard import clear_my_dashboard_cache


def invalidate_for_employee(doc, method=None):
	"""doc_events handler. Clears the dashboard cache for doc.employee.

	Also flushes ALL Team Dashboard caches: those are keyed by MANAGER, while this
	write is keyed by the team MEMBER, so a targeted clear would miss the manager whose
	team view just went stale. Resolving the member's managers here would mean walking
	reports_to on every write, so a blanket team flush is the cheaper trade (entries
	expire in 300s anyway).
	"""
	try:
		employee = getattr(doc, "employee", None)
		if employee:
			clear_my_dashboard_cache(employee)
		clear_my_dashboard_cache("team:")
	except Exception:
		# Never let cache invalidation break a transaction.
		frappe.log_error(
			title="my_dashboard cache invalidation failed",
			message=frappe.get_traceback(with_context=True),
		)


def invalidate_all(doc=None, method=None):
	"""Broad invalidation for org-wide masters (Holiday List, Employee)."""
	try:
		clear_my_dashboard_cache()
	except Exception:
		frappe.log_error(
			title="my_dashboard cache invalidation failed",
			message=frappe.get_traceback(with_context=True),
		)
