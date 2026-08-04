import frappe
import random
from frappe.model.document import Document
from frappe.utils import today, now_datetime

class RandomPresenceCheck(Document):
	def before_insert(self):
		if not self.check_date:
			self.check_date = today()
		if not self.time_initiated:
			self.time_initiated = now_datetime()
			
		# Populate employees if empty
		if not self.employees:
			self.populate_clocked_in_employees()

	def populate_clocked_in_employees(self):
		# Query checkins from today
		checkins = frappe.get_all(
			"Employee Checkin",
			filters={
				"time": [">=", today() + " 00:00:00"],
			},
			fields=["employee", "log_type", "time"],
			order_by="time asc"
		)

		# For each employee, get their last log type
		employee_status = {}
		for c in checkins:
			employee_status[c.employee] = c.log_type

		# Employees currently IN
		clocked_in = [emp for emp, status in employee_status.items() if status == "IN"]

		if not clocked_in:
			return

		# Apply selection criteria
		if self.selection_type == "Random Sample" and self.sample_size:
			size = min(int(self.sample_size), len(clocked_in))
			selected = random.sample(clocked_in, size)
		else:
			selected = clocked_in

		for emp_id in selected:
			emp_name = frappe.db.get_value("Employee", emp_id, "employee_name") or emp_id
			self.append("employees", {
				"employee": emp_id,
				"employee_name": emp_name,
				"status": "Pending"
			})

	def after_insert(self):
		# Trigger email/system notifications for selected employees
		for emp_row in self.employees:
			try:
				notify_employee_random_check(self, emp_row)
			except Exception:
				frappe.log_error(title="Random Check Notification Error", message=frappe.get_traceback())


def notify_employee_random_check(parent_doc, emp_row):
	# Get employee details
	user_id = frappe.db.get_value("Employee", emp_row.employee, "user_id")
	# Fallback to check standard fields
	email = (
		frappe.db.get_value("Employee", emp_row.employee, "prefered_email") or
		frappe.db.get_value("Employee", emp_row.employee, "personal_email") or
		frappe.db.get_value("Employee", emp_row.employee, "company_email") or
		user_id
	)

	# Format date/time nicely
	limit_time = frappe.utils.add_to_date(parent_doc.time_initiated, minutes=parent_doc.time_limit_minutes, as_datetime=True)
	time_str = frappe.utils.format_datetime(limit_time, "hh:mm a")

	if user_id:
		# Create a Frappe system notification (Notification Log)
		try:
			notification = frappe.get_doc({
				"doctype": "Notification Log",
				"for_user": user_id,
				"subject": "Random Presence Check Required",
				"email_content": f"You have been selected for a random presence check today. Please verify your presence by scanning your face/card at any terminal before {time_str} ({parent_doc.time_limit_minutes} minutes).",
				"document_type": "Random Presence Check",
				"document_name": parent_doc.name
			})
			notification.insert(ignore_permissions=True)
		except Exception:
			pass

	if email and "@" in email:
		# Send an email
		try:
			frappe.sendmail(
				recipients=[email],
				subject="[URGENT] Random Presence Check Required",
				message=f"""
				<p>Hello {emp_row.employee_name},</p>
				<p>You have been randomly selected for a presence check today.</p>
				<p><strong>Please scan your face or card at any Hikvision terminal within the next {parent_doc.time_limit_minutes} minutes.</strong></p>
				<p>Deadline: {time_str}</p>
				<p>Thank you,</p>
				<p>HR Department</p>
				"""
			)
		except Exception:
			pass
