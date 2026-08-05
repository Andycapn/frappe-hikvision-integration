# Copyright (c) 2026, Hikvision Integration and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, time_diff_in_hours, get_time

class AttendanceAnomaly(Document):
	def validate(self):
		if self.status == "Approved":
			if not self.hr_approved_out_time:
				frappe.throw(_("HR Approved OUT Time is mandatory when approving an Anomaly."))
			
			in_time = get_datetime(self.orphan_in_time)
			out_time = get_datetime(self.hr_approved_out_time)
			if out_time <= in_time:
				frappe.throw(_("HR Approved OUT Time ({0}) must be after Orphan IN Time ({1}).").format(out_time, in_time))

	def on_update(self):
		if self.status == "Approved" and self.attendance_reference and self.hr_approved_out_time:
			self.resolve_attendance_appeal()

	def resolve_attendance_appeal(self):
		if not frappe.db.exists("Attendance", self.attendance_reference):
			return

		att = frappe.get_doc("Attendance", self.attendance_reference)
		in_time = get_datetime(self.orphan_in_time)
		out_time = get_datetime(self.hr_approved_out_time)

		working_hours = time_diff_in_hours(out_time, in_time)

		# Fetch matched shift configuration to evaluate late entry & early exit
		late_entry = 0
		early_exit = 0
		
		if self.matched_shift and frappe.db.exists("Shift Type", self.matched_shift):
			shift = frappe.get_doc("Shift Type", self.matched_shift)
			
			# Check late entry
			st_time = get_time(shift.start_time)
			shift_start_dt = in_time.replace(hour=st_time.hour, minute=st_time.minute, second=st_time.second)
			late_grace = shift.late_entry_grace_period or 0
			if (in_time - shift_start_dt).total_seconds() > (late_grace * 60):
				late_entry = 1

			# Check early exit
			et_time = get_time(shift.end_time)
			shift_end_dt = out_time.replace(hour=et_time.hour, minute=et_time.minute, second=et_time.second)
			early_grace = shift.early_exit_grace_period or 0
			if (shift_end_dt - out_time).total_seconds() > (early_grace * 60):
				early_exit = 1

		# Determine status
		new_status = "Present"
		if working_hours < 4.0 and working_hours > 0:
			new_status = "Half Day"

		# Update Attendance
		if att.docstatus == 1:
			frappe.db.set_value("Attendance", att.name, {
				"status": new_status,
				"out_time": out_time,
				"working_hours": round(working_hours, 2),
				"late_entry": late_entry,
				"early_exit": early_exit,
				"custom_is_anomaly": 0
			})
		else:
			att.status = new_status
			att.out_time = out_time
			att.working_hours = round(working_hours, 2)
			att.late_entry = late_entry
			att.early_exit = early_exit
			att.custom_is_anomaly = 0
			att.save(ignore_permissions=True)

		frappe.msgprint(_("Attendance {0} updated to {1} with {2:.2f} working hours.").format(att.name, new_status, working_hours))
