import frappe
from frappe.model.document import Document
from frappe.utils import flt, date_diff, getdate

class AttendanceDeductionReview(Document):
	@frappe.whitelist()
	def fetch_employees_for_review(self):
		"""
		Load all active employees for the selected company,
		compute their checkins based on Frappe's holiday list standard working days,
		evaluate deduction rules, and populate the child table.
		"""
		self.set("deductions", [])

		if not self.company or not self.start_date or not self.end_date:
			frappe.throw("Please set Company, Start Date and End Date.")

		# Get rules sorted descending by min_missed_days
		rules = sorted(self.get("rules", []), key=lambda r: r.min_missed_days, reverse=True)

		active_employees = frappe.get_all(
			"Employee",
			filters={"status": "Active", "company": self.company},
			fields=["name", "employee_name", "holiday_list"]
		)
		
		# Fallback to company holiday list
		company_holiday_list = frappe.db.get_value("Company", self.company, "default_holiday_list")

		for emp in active_employees:
			holiday_list = emp.holiday_list or company_holiday_list
			
			total_days_in_period = date_diff(self.end_date, self.start_date) + 1
			holiday_count = 0
			
			if holiday_list:
				holiday_count = frappe.db.count("Holiday", filters={
					"parent": holiday_list,
					"holiday_date": ["between", [self.start_date, self.end_date]]
				})
			
			total_working_days = total_days_in_period - holiday_count
			if total_working_days <= 0:
				continue
				
			# Check actual check-ins for period
			checkin_days = frappe.db.sql_list(
				"""
				SELECT DISTINCT DATE(time)
				FROM `tabEmployee Checkin`
				WHERE employee = %s
				  AND DATE(time) BETWEEN %s AND %s
				""",
				(emp.name, self.start_date, self.end_date)
			)
			actual_days = len(checkin_days)
			missed_days = max(0.0, float(total_working_days) - actual_days)
			
			# Check if employee has ANY historical checkins
			historical_checkins = frappe.db.count("Employee Checkin", filters={"employee": emp.name})
			has_historical = 1 if historical_checkins > 0 else 0
			
			apply_deduction = 0
			matched_percentage = 0.0
			
			if has_historical and missed_days > 0:
				for rule in rules:
					if missed_days >= rule.min_missed_days:
						matched_percentage = rule.deduction_percentage
						apply_deduction = 1
						break
			
			self.append("deductions", {
				"employee": emp.name,
				"employee_name": emp.employee_name,
				"total_working_days": total_working_days,
				"actual_days_checked_in": actual_days,
				"missed_days": missed_days,
				"has_historical_checkins": has_historical,
				"matched_percentage": matched_percentage,
				"apply_deduction": apply_deduction,
				"on_leave": 0
			})

		return self.deductions
