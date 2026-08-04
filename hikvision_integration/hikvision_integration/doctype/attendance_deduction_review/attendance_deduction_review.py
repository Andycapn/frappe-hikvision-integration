import frappe
from frappe.model.document import Document
from frappe.utils import flt

class AttendanceDeductionReview(Document):
	@frappe.whitelist()
	def fetch_draft_salary_slip_deductions(self):
		"""
		Load all draft Salary Slips for the selected company and period,
		compute their checkins/deductions, and populate the child table.
		"""
		# Clear existing deductions
		self.set("deductions", [])

		# Find all draft Salary Slips matching start_date, end_date and company
		draft_slips = frappe.get_all(
			"Salary Slip",
			filters={
				"docstatus": 0,
				"company": self.company,
				"start_date": self.start_date,
				"end_date": self.end_date
			},
			fields=["name", "employee", "employee_name", "total_working_days"]
		)

		if not draft_slips:
			return []

		for slip_data in draft_slips:
			slip = frappe.get_doc("Salary Slip", slip_data.name)
			
			# Count distinct checkin dates
			checkin_days = frappe.db.sql_list(
				"""
				SELECT DISTINCT DATE(time)
				FROM `tabEmployee Checkin`
				WHERE employee = %s
				  AND DATE(time) BETWEEN %s AND %s
				""",
				(slip.employee, self.start_date, self.end_date)
			)
			actual_days = len(checkin_days)
			total_days = flt(slip.total_working_days)

			if total_days <= 0:
				continue

			# Sum gross earnings
			gross_earnings = sum(flt(d.amount) for d in slip.earnings)
			
			# Calculate deduction
			percentage = flt(self.deduction_percentage) if self.deduction_percentage is not None else 100.0
			if actual_days < total_days:
				missed_days = total_days - actual_days
				daily_rate = gross_earnings / total_days
				calculated_deduction = daily_rate * missed_days * (percentage / 100.0)
			else:
				missed_days = 0
				calculated_deduction = 0.0

			# Append to child table
			self.append("deductions", {
				"employee": slip.employee,
				"employee_name": slip.employee_name,
				"total_working_days": total_days,
				"actual_days_checked_in": actual_days,
				"missed_days": missed_days,
				"gross_pay": gross_earnings,
				"calculated_deduction": flt(calculated_deduction, 2),
				"approved_deduction": flt(calculated_deduction, 2),
				"approved": 1 if calculated_deduction > 0 else 0
			})

		return self.deductions
