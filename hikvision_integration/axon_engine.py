# Copyright (c) 2026, Hikvision Integration and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import (
	get_datetime,
	getdate,
	add_days,
	time_diff_in_hours,
	get_time,
	cint,
	flt
)
from datetime import datetime, timedelta

DEPARTMENT_SHIFT_MATRIX = {
	"Security": ["Security Day Shift", "Security Night Shift"],
	"Gardeners": ["Standard Day Shift"],
	"Housekeeping & Laundry": ["Standard Day Shift", "Operations Morning Shift", "Operations Afternoon Shift"],
	"Administration": ["Standard Day Shift"],
	"Finance": ["Standard Day Shift"],
	"Maintenance": ["Standard Day Shift"],
	"Food & Beverages": ["Operations Morning Shift", "Operations Afternoon Shift", "Operations Night Shift"],
	"Kitchen": ["Operations Morning Shift", "Operations Afternoon Shift", "Operations Night Shift"],
	"Front Office": ["Operations Morning Shift", "Operations Afternoon Shift", "Operations Night Shift"],
	"Transport & Logistics": ["Operations Morning Shift", "Operations Afternoon Shift", "Operations Night Shift"],
	"Game": ["Operations Morning Shift", "Operations Afternoon Shift", "Operations Night Shift"]
}

EXEMPT_SHIFTS = ["Field / On Duty"]

def setup_custom_fields():
	"""Ensure custom fields exist on Attendance DocType."""
	custom_fields = {
		"Attendance": [
			{
				"fieldname": "custom_is_anomaly",
				"fieldtype": "Check",
				"label": "Is Anomaly",
				"default": 0,
				"read_only": 1,
				"insert_after": "status"
			},
			{
				"fieldname": "custom_anomaly_reference",
				"fieldtype": "Link",
				"options": "Attendance Anomaly",
				"label": "Anomaly Reference",
				"read_only": 1,
				"insert_after": "custom_is_anomaly"
			}
		]
	}
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
	create_custom_fields(custom_fields)

def get_candidate_shifts(department):
	"""Phase 2: Return authorized candidate shifts for department."""
	if not department:
		return get_all_active_shifts()
		
	for dept_key, shift_list in DEPARTMENT_SHIFT_MATRIX.items():
		if dept_key.lower() in department.lower():
			return shift_list
			
	return get_all_active_shifts()

def get_all_active_shifts():
	return ["Operations Morning Shift", "Operations Afternoon Shift", "Operations Night Shift", "Security Day Shift", "Security Night Shift", "Standard Day Shift"]

def time_to_float_hours(dt):
	"""Convert datetime or string time to float hours from midnight."""
	if isinstance(dt, datetime):
		return dt.hour + (dt.minute / 60.0) + (dt.second / 3600.0)
	elif isinstance(dt, str):
		t_parts = list(map(int, dt.split(':')))
		return t_parts[0] + (t_parts[1] / 60.0) + (t_parts[2] / 3600.0 if len(t_parts) > 2 else 0)
	return 0.0

def score_shift(in_time, out_time, shift_name):
	"""
	Phase 3: Weighted Deviation Scoring Algorithm
	Score = 0.75 * |T_in - S_start| + 0.25 * |T_out - S_end|
	"""
	if not frappe.db.exists("Shift Type", shift_name):
		return 999999.0
		
	shift = frappe.get_doc("Shift Type", shift_name)
	
	t_in_hrs = time_to_float_hours(in_time)
	s_start_hrs = time_to_float_hours(shift.start_time)
	
	in_diff = abs(t_in_hrs - s_start_hrs)
	if in_diff > 12: # Midnight wraparound
		in_diff = 24 - in_diff

	out_diff = 0.0
	if out_time:
		t_out_hrs = time_to_float_hours(out_time)
		s_end_hrs = time_to_float_hours(shift.end_time)
		out_diff = abs(t_out_hrs - s_end_hrs)
		if out_diff > 12:
			out_diff = 24 - out_diff
			
	score = (0.75 * in_diff) + (0.25 * out_diff)
	return score

def lock_best_shift(in_time, out_time, candidate_shifts):
	"""Find candidate shift with lowest deviation score."""
	best_shift = None
	lowest_score = 999999.0

	for s_name in candidate_shifts:
		sc = score_shift(in_time, out_time, s_name)
		if sc < lowest_score:
			lowest_score = sc
			best_shift = s_name

	return best_shift or candidate_shifts[0]

def sessionize_checkins(employee, from_date=None, to_date=None):
	"""
	Phase 1: Chronological Aggregation & 14-Hour Sliding Window Sessionizer
	"""
	filters = {"employee": employee}
	if from_date and to_date:
		filters["time"] = ["between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]]

	checkins = frappe.get_all("Employee Checkin",
		filters=filters,
		fields=["name", "time", "log_type"],
		order_by="time asc"
	)

	if not checkins:
		return []

	sessions = []
	i = 0
	n = len(checkins)

	while i < n:
		anchor = checkins[i]
		t_in = get_datetime(anchor["time"])
		
		# 14-hour sliding window boundary
		window_end = t_in + timedelta(hours=14)
		
		t_out = None
		out_log_name = None
		j = i + 1
		
		while j < n:
			curr = checkins[j]
			curr_t = get_datetime(curr["time"])
			
			if curr_t > window_end:
				break # Window expired
				
			# Skip duplicate IN punch within 2 hours of anchor
			if curr["log_type"] == "IN" and (curr_t - t_in).total_seconds() < 7200:
				j += 1
				continue
				
			if curr["log_type"] == "OUT":
				t_out = curr_t
				out_log_name = curr["name"]
				i = j # Advance pointer past this OUT punch
				break
				
			j += 1

		if t_out:
			sessions.append({
				"in_time": t_in,
				"out_time": t_out,
				"in_name": anchor["name"],
				"out_name": out_log_name,
				"type": "COMPLETE"
			})
		else:
			# Orphaned IN
			sessions.append({
				"in_time": t_in,
				"out_time": None,
				"in_name": anchor["name"],
				"out_name": None,
				"type": "ORPHAN"
			})
		i += 1

	return sessions

def create_or_update_attendance(employee, company, attendance_date, status, in_time, out_time, shift, is_anomaly=0):
	"""Generate or update Attendance document."""
	working_hours = 0.0
	late_entry = 0
	early_exit = 0

	if out_time:
		working_hours = time_diff_in_hours(out_time, in_time)
		
		if shift and frappe.db.exists("Shift Type", shift):
			sh_doc = frappe.get_doc("Shift Type", shift)
			
			# Late Entry Check
			st_time = get_time(sh_doc.start_time)
			shift_start_dt = in_time.replace(hour=st_time.hour, minute=st_time.minute, second=st_time.second)
			late_grace = sh_doc.late_entry_grace_period or 0
			if (in_time - shift_start_dt).total_seconds() > (late_grace * 60):
				late_entry = 1

			# Early Exit Check
			et_time = get_time(sh_doc.end_time)
			shift_end_dt = out_time.replace(hour=et_time.hour, minute=et_time.minute, second=et_time.second)
			early_grace = sh_doc.early_exit_grace_period or 0
			if (shift_end_dt - out_time).total_seconds() > (early_grace * 60):
				early_exit = 1

	existing = frappe.get_all("Attendance", filters={"employee": employee, "attendance_date": attendance_date}, fields=["name"])
	
	if existing:
		att = frappe.get_doc("Attendance", existing[0]["name"])
		att.status = status
		att.in_time = in_time
		att.out_time = out_time
		att.shift = shift
		att.working_hours = round(working_hours, 2)
		att.late_entry = late_entry
		att.early_exit = early_exit
		att.custom_is_anomaly = is_anomaly
		att.save(ignore_permissions=True)
		return att.name
	else:
		att = frappe.get_doc({
			"doctype": "Attendance",
			"employee": employee,
			"company": company,
			"attendance_date": attendance_date,
			"status": status,
			"in_time": in_time,
			"out_time": out_time,
			"shift": shift,
			"working_hours": round(working_hours, 2),
			"late_entry": late_entry,
			"early_exit": early_exit,
			"custom_is_anomaly": is_anomaly
		})
		att.insert(ignore_permissions=True)
		return att.name

def create_attendance_anomaly(employee, attendance_date, matched_shift, orphan_in_time, attendance_reference):
	"""Create custom Attendance Anomaly record for HR appeal."""
	existing = frappe.get_all("Attendance Anomaly", filters={"employee": employee, "attendance_date": attendance_date}, fields=["name"])
	if existing:
		return existing[0]["name"]

	anomaly = frappe.get_doc({
		"doctype": "Attendance Anomaly",
		"employee": employee,
		"attendance_date": attendance_date,
		"matched_shift": matched_shift,
		"orphan_in_time": orphan_in_time,
		"anomaly_reason": "Missing OUT Punch",
		"status": "Pending Appeal",
		"attendance_reference": attendance_reference
	})
	anomaly.insert(ignore_permissions=True)
	
	# Link back to Attendance record
	frappe.db.set_value("Attendance", attendance_reference, "custom_anomaly_reference", anomaly.name)
	return anomaly.name

@frappe.whitelist()
def process_axon_attendance(from_date=None, to_date=None, employee=None):
	"""
	Main Entry Point for Axon Dynamic Attendance Engine.
	"""
	setup_custom_fields()

	emp_filters = {"status": "Active"}
	if employee:
		emp_filters["name"] = employee

	employees = frappe.get_all("Employee", filters=emp_filters, fields=["name", "employee_name", "department", "company"])
	
	total_processed = 0

	for emp in employees:
		dept = emp["department"]
		candidate_shifts = get_candidate_shifts(dept)
		sessions = sessionize_checkins(emp["name"], from_date, to_date)

		for sess in sessions:
			in_time = sess["in_time"]
			out_time = sess["out_time"]
			att_date = in_time.date()

			locked_shift = lock_best_shift(in_time, out_time, candidate_shifts)

			if sess["type"] == "COMPLETE":
				create_or_update_attendance(
					employee=emp["name"],
					company=emp["company"],
					attendance_date=att_date,
					status="Present",
					in_time=in_time,
					out_time=out_time,
					shift=locked_shift,
					is_anomaly=0
				)
				total_processed += 1
			elif sess["type"] == "ORPHAN":
				if locked_shift in EXEMPT_SHIFTS:
					auto_out = in_time + timedelta(hours=8)
					create_or_update_attendance(
						employee=emp["name"],
						company=emp["company"],
						attendance_date=att_date,
						status="Present",
						in_time=in_time,
						out_time=auto_out,
						shift=locked_shift,
						is_anomaly=0
					)
					total_processed += 1
				else:
					att_name = create_or_update_attendance(
						employee=emp["name"],
						company=emp["company"],
						attendance_date=att_date,
						status="Absent",
						in_time=in_time,
						out_time=None,
						shift=locked_shift,
						is_anomaly=1
					)

					create_attendance_anomaly(
						employee=emp["name"],
						attendance_date=att_date,
						matched_shift=locked_shift,
						orphan_in_time=in_time,
						attendance_reference=att_name
					)
					total_processed += 1

	frappe.db.commit()
	return f"Axon Engine processed {total_processed} attendance sessions successfully."
