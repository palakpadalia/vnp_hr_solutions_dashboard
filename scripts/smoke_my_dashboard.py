"""Smoke test for the My Dashboard read-API.

Exercises all 6 endpoints as a real user and prints a pass/fail table.

With --seed it inserts a month of Attendance fixtures so the widgets return
non-empty data, then ALWAYS rolls back -- nothing is committed.

Usage (from the bench's sites/ directory):

    cd ~/vnp/sites
    ../env/bin/python ../apps/vnp_hr_solutions_dashboard/scripts/smoke_my_dashboard.py \
        --site vnp.local --user palak@gmail.com

    # with fixture data for June 2026
    ../env/bin/python ../apps/vnp_hr_solutions_dashboard/scripts/smoke_my_dashboard.py \
        --site vnp.local --user palak@gmail.com --seed --month 6 --year 2026

    # verify the no-Employee edge case
    ../env/bin/python ../apps/vnp_hr_solutions_dashboard/scripts/smoke_my_dashboard.py \
        --site vnp.local --user Administrator
"""

import argparse
import datetime
import json
import sys
import time

import frappe

ENDPOINTS = [
	("get_my_dashboard", {}),
	("get_attendance_calendar", {}),
	("get_pending_requests", {"limit": 20}),
	("get_announcements", {"limit": 10}),
	("get_birthdays", {}),
	("get_anniversaries", {}),
]

# day-of-month -> Attendance.status
FIXTURE = [
	(1, "Present"),
	(2, "Present"),
	(3, "Present"),
	(4, "Present"),
	(5, "Present"),
	(8, "Work From Home"),
	(9, "Half Day"),
	(10, "On Leave"),
	(11, "Absent"),
]


def seed(employee, company, month, year):
	made = 0
	for day, status in FIXTURE:
		try:
			d = frappe.get_doc(
				{
					"doctype": "Attendance",
					"employee": employee,
					"company": company,
					"attendance_date": datetime.date(year, month, day).isoformat(),
					"status": status,
				}
			)
			d.flags.ignore_permissions = True
			d.flags.ignore_mandatory = True
			d.insert(ignore_permissions=True)
			d.submit()
			made += 1
		except Exception as e:
			print(f"    ! skipped {year}-{month:02d}-{day:02d} {status}: {type(e).__name__}")
	return made


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--site", required=True)
	ap.add_argument("--user", default="Administrator")
	ap.add_argument("--month", type=int, default=None)
	ap.add_argument("--year", type=int, default=None)
	ap.add_argument("--seed", action="store_true", help="insert Attendance fixtures, then roll back")
	ap.add_argument("--json", action="store_true", help="dump full bulk payload")
	args = ap.parse_args()

	frappe.init(site=args.site, sites_path=".")
	frappe.connect()

	failures = 0
	try:
		from vnp_hr_solutions_dashboard.dashboard.api import my_dashboard as md

		emp = frappe.db.get_value(
			"Employee", {"user_id": args.user}, ["name", "company"], as_dict=True
		)
		print(f"site={args.site}  user={args.user}  employee={emp.name if emp else None}")

		if args.seed:
			if not emp:
				print("  --seed needs a user with a linked Employee; skipping fixtures.")
			else:
				month = args.month or datetime.date.today().month
				year = args.year or datetime.date.today().year
				frappe.set_user("Administrator")
				n = seed(emp.name, emp.company, month, year)
				print(f"  seeded {n} Attendance rows for {year}-{month:02d} (will roll back)")

		md.clear_my_dashboard_cache()
		frappe.set_user(args.user)

		window = {}
		if args.month:
			window["month"] = args.month
		if args.year:
			window["year"] = args.year

		print()
		print(f"{'endpoint':28s} {'ms':>7s}  {'status':8s}  summary")
		print("-" * 100)

		for name, kwargs in ENDPOINTS:
			fn = getattr(md, name)
			call = dict(kwargs)
			# only pass month/year to endpoints that accept them
			if window:
				code = fn.__wrapped__.__code__ if hasattr(fn, "__wrapped__") else fn.__code__
				for k, v in window.items():
					if k in code.co_varnames[: code.co_argcount]:
						call[k] = v
			t0 = time.time()
			try:
				res = fn(**call)
				ms = (time.time() - t0) * 1000
				data = res.get("data")
				if isinstance(data, list):
					summary = f"{len(data)} items"
				elif isinstance(data, dict):
					if "data" in data and isinstance(data["data"], list):
						summary = f"{len(data['data'])} rows"
					else:
						summary = ", ".join(list(data.keys())[:6])
				else:
					summary = type(data).__name__
				print(f"{name:28s} {ms:7.1f}  {res.get('status'):8s}  {summary}")
			except Exception as e:
				failures += 1
				ms = (time.time() - t0) * 1000
				print(f"{name:28s} {ms:7.1f}  {'FAIL':8s}  {type(e).__name__}: {e}")

		# cache effectiveness
		md.clear_my_dashboard_cache()
		t0 = time.time()
		md.get_my_dashboard(**window)
		cold = time.time() - t0
		t0 = time.time()
		md.get_my_dashboard(**window)
		warm = time.time() - t0
		print("-" * 100)
		print(f"cache: cold={cold * 1000:.1f}ms  warm={warm * 1000:.1f}ms  speedup={cold / max(warm, 1e-9):.1f}x")

		bulk = md.get_my_dashboard(**window)
		d = bulk["data"]
		print()
		print("KPIs:      ", json.dumps([(k["label"], k["value"]) for k in d["kpis"]], default=str))
		print(
			"CAL stats: ",
			json.dumps([(s["label"], s["value"]) for s in d["attendance_calendar"]["stats"]]),
		)
		print("CAL types: ", json.dumps(sorted({e["type"] for e in d["attendance_calendar"]["data"]})))
		print("leave rows:", len(d["leave_balance_chart"]["data"]))
		print("pending:   ", len(d["pending_requests"]["data"]))
		print("birthdays: ", len(d["birthdays"]["data"]), " anniversaries:", len(d["anniversaries"]["data"]))
		if bulk["meta"].get("warnings"):
			print()
			for w in bulk["meta"]["warnings"]:
				print("WARN:", w)

		if args.json:
			print()
			print(json.dumps(bulk, indent=2, default=str))

	finally:
		frappe.db.rollback()
		if args.seed:
			print()
			print(f"rolled back -- Attendance count is {frappe.db.count('Attendance')}")
		frappe.destroy()

	print()
	print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures} endpoint(s))")
	sys.exit(1 if failures else 0)


if __name__ == "__main__":
	main()
