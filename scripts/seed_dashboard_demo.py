"""Seed realistic dashboard demo data on a DEV/TEST bench, and remove it again.

Idempotent: every record is created only if an equivalent one is absent, and every
created name is printed so it can be rolled back.

Pair (verified before use, not assumed):
    MANAGER = HR-EMP-00001  (palak@gmail.com)          -- Team Dashboard caller
    MEMBER  = HR-EMP-00020  (palakudi11e2e2@gmail.com) -- My Dashboard caller, reports to MANAGER

Target month: July 2026 (past, so month_to_date == full month).
July 2026 has 23 working days: weekly_off = Sunday on the Holiday List, and the API's
weekend reconciliation adds Saturday, giving 8 weekend days.

Usage (from the bench's sites/ directory):

    ../env/bin/python ../apps/vnp_hr_solutions_dashboard/scripts/seed_dashboard_demo.py \
        --site vnp.local --seed
    ../env/bin/python ../apps/vnp_hr_solutions_dashboard/scripts/seed_dashboard_demo.py \
        --site vnp.local --remove

NEVER run --seed against production. It creates submitted Attendance and an LWP Leave Type.
"""

import argparse
import datetime
import sys

import frappe

MANAGER = "HR-EMP-00001"
MEMBER = "HR-EMP-00020"
COMPANY = "V&P HR Solutions Pvt. Ltd."
YEAR, MONTH = 2026, 7

LWP_TYPE = "Leave Without Pay (Test)"
PAID_TYPE = "Casual"

# day -> (Attendance.status, leave_type or None)
PLAN = {
	1: ("Present", None),
	2: ("Present", None),
	3: ("Present", None),
	6: ("Present", None),
	7: ("Present", None),
	8: ("Present", None),
	9: ("Present", None),
	10: ("Work From Home", None),
	13: ("Half Day", None),
	14: ("On Leave", LWP_TYPE),  # unpaid
	15: ("On Leave", PAID_TYPE),  # paid
	# 16,17,20..31 deliberately left unmarked
}

MARKER = "vnp-dashboard-demo"  # written into free-text fields so --remove can find rows


def _created(label, name):
	print(f"  CREATED  {label:34s} {name}")


def _skipped(label, name):
	print(f"  exists   {label:34s} {name}")


def seed():
	created = []

	# --- LWP leave type -----------------------------------------------------
	if frappe.db.exists("Leave Type", LWP_TYPE):
		_skipped("Leave Type (LWP)", LWP_TYPE)
	else:
		lt = frappe.get_doc(
			{
				"doctype": "Leave Type",
				"leave_type_name": LWP_TYPE,
				"is_lwp": 1,
				"max_leaves_allowed": 30,
				"include_holiday": 0,
			}
		)
		lt.flags.ignore_permissions = True
		lt.insert(ignore_permissions=True)
		created.append(("Leave Type", lt.name))
		_created("Leave Type (LWP)", lt.name)

	# --- Attendance ---------------------------------------------------------
	for day, (status, leave_type) in sorted(PLAN.items()):
		d = datetime.date(YEAR, MONTH, day)
		existing = frappe.db.exists(
			"Attendance", {"employee": MEMBER, "attendance_date": d, "docstatus": ["<", 2]}
		)
		if existing:
			_skipped(f"Attendance {d} {status}", existing)
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Attendance",
				"employee": MEMBER,
				"company": COMPANY,
				"attendance_date": d.isoformat(),
				"status": status,
			}
		)
		if leave_type:
			doc.leave_type = leave_type
		doc.flags.ignore_permissions = True
		doc.flags.ignore_mandatory = True
		try:
			doc.insert(ignore_permissions=True)
			doc.submit()
			created.append(("Attendance", doc.name))
			_created(f"Attendance {d} {status}", doc.name)
		except Exception as e:
			print(f"  FAILED   Attendance {d} {status}: {type(e).__name__}: {str(e)[:110]}")

	# NOTE: an LWP Leave Allocation is intentionally NOT seeded -- HRMS refuses it
	# ("Leave Type ... cannot be allocated since it is leave without pay"). The leave
	# balance chart is built from allocations, so `is_lwp` there is always false by
	# design; the LWP effect shows up in the calendar totals as unpaid_leave instead.

	# --- Leave Application (Open) ------------------------------------------
	if frappe.db.exists(
		"Leave Application", {"employee": MEMBER, "from_date": f"{YEAR}-07-28", "docstatus": 0}
	):
		_skipped("Leave Application (Open)", "already present")
	else:
		try:
			la = frappe.get_doc(
				{
					"doctype": "Leave Application",
					"employee": MEMBER,
					"leave_type": PAID_TYPE,
					"from_date": f"{YEAR}-07-28",
					"to_date": f"{YEAR}-07-29",
					"company": COMPANY,
					"status": "Open",
					"posting_date": f"{YEAR}-07-20",
					"description": MARKER,
					"leave_approver": "palak@gmail.com",
				}
			)
			la.flags.ignore_permissions = True
			la.flags.ignore_mandatory = True
			la.insert(ignore_permissions=True)
			created.append(("Leave Application", la.name))
			_created("Leave Application (Open)", la.name)
		except Exception as e:
			print(f"  FAILED   Leave Application: {type(e).__name__}: {str(e)[:110]}")

	# --- Attendance Request (WFH) ------------------------------------------
	if frappe.db.exists(
		"Attendance Request", {"employee": MEMBER, "from_date": f"{YEAR}-07-24"}
	):
		_skipped("Attendance Request (WFH)", "already present")
	else:
		try:
			ar = frappe.get_doc(
				{
					"doctype": "Attendance Request",
					"employee": MEMBER,
					"company": COMPANY,
					"from_date": f"{YEAR}-07-24",
					"to_date": f"{YEAR}-07-24",
					"reason": "Work From Home",
					"explanation": MARKER,
				}
			)
			ar.flags.ignore_permissions = True
			ar.flags.ignore_mandatory = True
			ar.insert(ignore_permissions=True)
			created.append(("Attendance Request", ar.name))
			_created("Attendance Request (WFH)", ar.name)
		except Exception as e:
			print(f"  FAILED   Attendance Request: {type(e).__name__}: {str(e)[:110]}")

	frappe.db.commit()
	print(f"\n  {len(created)} record(s) created this run.")
	return created


def remove():
	"""Cancel + delete the seeded rows, newest dependency first."""
	removed = 0
	start, end = f"{YEAR}-07-01", f"{YEAR}-07-31"

	for dt, filters in (
		("Attendance Request", {"employee": MEMBER, "from_date": f"{YEAR}-07-24"}),
		("Leave Application", {"employee": MEMBER, "from_date": f"{YEAR}-07-28"}),
		(
			"Attendance",
			{"employee": MEMBER, "attendance_date": ["between", [start, end]]},
		),
	):
		for name in frappe.get_all(dt, filters=filters, pluck="name"):
			doc = frappe.get_doc(dt, name)
			if doc.docstatus == 1:
				doc.cancel()
			doc.delete(ignore_permissions=True)
			removed += 1
			print(f"  removed  {dt:22s} {name}")

	# Leave Type last: Attendance rows reference it.
	if frappe.db.exists("Leave Type", LWP_TYPE):
		try:
			frappe.delete_doc("Leave Type", LWP_TYPE, ignore_permissions=True)
			removed += 1
			print(f"  removed  {'Leave Type':22s} {LWP_TYPE}")
		except Exception as e:
			print(f"  kept     Leave Type {LWP_TYPE}: still linked ({type(e).__name__})")

	frappe.db.commit()
	print(f"\n  {removed} record(s) removed.")


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--site", required=True)
	g = ap.add_mutually_exclusive_group(required=True)
	g.add_argument("--seed", action="store_true")
	g.add_argument("--remove", action="store_true")
	args = ap.parse_args()

	frappe.init(site=args.site, sites_path=".")
	frappe.connect()
	frappe.set_user("Administrator")
	try:
		print(f"site={args.site}  manager={MANAGER}  member={MEMBER}  month={YEAR}-{MONTH:02d}")
		if args.seed:
			seed()
		else:
			remove()
	finally:
		frappe.destroy()
	return 0


if __name__ == "__main__":
	sys.exit(main())
