import frappe
from frappe import _
from frappe.utils import flt, getdate

def check_and_create_salary_component():
	"""
	Ensure the 'Attendance Deduction' Salary Component exists in the database.
	"""
	if not frappe.db.exists("Salary Component", "Attendance Deduction"):
		frappe.get_doc({
			"doctype": "Salary Component",
			"salary_component": "Attendance Deduction",
			"salary_component_abbr": "ATTD",
			"type": "Deduction"
		}).insert(ignore_permissions=True)
		frappe.db.commit()


def calculate_attendance_deduction(doc, method=None):
	"""
	Hook running on Salary Slip before_save.
	Calculates or retrieves approved attendance deduction.
	"""
	if not (doc.employee and doc.start_date and doc.end_date):
		return

	# Ensure Salary Component exists
	check_and_create_salary_component()

	# Check if an Attendance Deduction Review exists for this company and period
	# (Prefer Submitted over Draft if both exist)
	review_name = frappe.db.get_value(
		"Attendance Deduction Review",
		{
			"company": doc.company,
			"start_date": doc.start_date,
			"end_date": doc.end_date,
			"docstatus": ["in", [0, 1]]
		},
		"name",
		order_by="docstatus desc"
	)

	if review_name:
		# Retrieve the approved deduction amount for the employee
		detail = frappe.db.get_value(
			"Attendance Deduction Review Detail",
			{
				"parent": review_name,
				"employee": doc.employee
			},
			["approved", "approved_deduction"],
			as_dict=True
		)
		if detail and detail.approved:
			deduction_amount = flt(detail.approved_deduction)
		else:
			deduction_amount = 0.0
	else:
		# Fallback to on-the-fly calculation (initial proposal)
		checkin_days = frappe.db.sql_list(
			"""
			SELECT DISTINCT DATE(time)
			FROM `tabEmployee Checkin`
			WHERE employee = %s
			  AND DATE(time) BETWEEN %s AND %s
			""",
			(doc.employee, doc.start_date, doc.end_date),
		)
		actual_days = len(checkin_days)
		total_days = flt(doc.total_working_days)

		if total_days > 0:
			deduction_percentage = flt(frappe.conf.get("hikvision_attendance_deduction_percentage"), 100.0)
			gross_earnings = sum(flt(d.amount) for d in doc.earnings)

			if actual_days < total_days:
				missed_days = total_days - actual_days
				daily_rate = gross_earnings / total_days
				deduction_amount = daily_rate * missed_days * (deduction_percentage / 100.0)
			else:
				deduction_amount = 0.0
		else:
			deduction_amount = 0.0

	# Round the deduction amount
	deduction_amount = flt(deduction_amount, 2)

	# Update or append the "Attendance Deduction" in doc.deductions table
	updated = False
	for d in doc.deductions:
		if d.salary_component == "Attendance Deduction":
			d.amount = deduction_amount
			d.base_amount = deduction_amount
			updated = True
			break

	if not updated and deduction_amount > 0:
		doc.append(
			"deductions",
			{
				"salary_component": "Attendance Deduction",
				"abbr": "ATTD",
				"amount": deduction_amount,
				"base_amount": deduction_amount,
				"category": "Deduction",
			},
		)

	# Recalculate totals
	if hasattr(doc, "calculate_salary_slip"):
		doc.calculate_salary_slip()
