import json
import frappe
from frappe import _

@frappe.whitelist(allow_guest=True)
def webhook():
	payload = None
	image_file = None

	# 1. Try to get JSON from request (standard JSON body)
	try:
		payload = frappe.request.get_json()
	except Exception:
		pass

	# 2. Try to get from multipart form data (Hikvision standard with images)
	if not payload:
		try:
			# Hikvision sends JSON in a form field.
			# Different models use different names: 'AccessControllerEvent' or 'event_log'
			for field in ["AccessControllerEvent", "event_log"]:
				event_json = frappe.request.form.get(field)
				if event_json:
					payload = json.loads(event_json)
					break

			# Check for image in files
			if "Picture" in frappe.request.files:
				image_file = frappe.request.files["Picture"]
		except Exception:
			pass

	# 3. Fallback to raw data parsing if headers are missing/incorrect
	if not payload:
		try:
			data = frappe.request.get_data(as_text=True)
			if data:
				# Check if it's multipart but not parsed correctly
				if "--MIME_boundary" in data:
					# Simple manual extract if Werkzeug failed for some reason
					parts = data.split("--MIME_boundary")
					for part in parts:
						if 'name="AccessControllerEvent"' in part or 'name="event_log"' in part:
							# Find the start of JSON
							content_start = part.find("{")
							content_end = part.rfind("}")
							if content_start != -1 and content_end != -1:
								payload = json.loads(part[content_start:content_end+1])
								break
				else:
					payload = json.loads(data)
		except Exception:
			pass

	if not payload:
		frappe.log_error(
			title=_("Hikvision Webhook No Payload"),
			message=f"Headers: {frappe.request.headers}\nForm Keys: {list(frappe.request.form.keys()) if frappe.request.form else 'None'}\nData Preview: {frappe.request.get_data(as_text=True)[:500]}"
		)
		return {"status": "error", "message": "No payload received"}

	frappe.log_error(
		title=_("Hikvision Debug Payload"),
		message=json.dumps(payload, indent=2)
	)

	# Handle heartbeat events
	if payload.get("eventType") == "heartBeat":
		device_serial = payload.get("shortSerialNumber")
		if device_serial:
			if not frappe.db.exists("Hikvision Device", device_serial):
				device_doc = frappe.get_doc({
					"doctype": "Hikvision Device",
					"device_serial": device_serial,
					"device_name": f"Device {device_serial}",
					"ip_address": payload.get("ipAddress")
				})
				device_doc.insert(ignore_permissions=True)
				frappe.db.commit()

			frappe.db.set_value("Hikvision Device", device_serial, "last_heartbeat", frappe.utils.now_datetime())
			frappe.db.commit()
		return {"status": "received", "message": "Heartbeat updated"}

	event_data = payload.get("AccessControllerEvent", {})

	# Filter out events with undefined attendance status
	if event_data.get("attendanceStatus") in ["undefined", None] and payload.get("eventType") != "heartBeat":
		return {"status": "received", "message": "Non-attendance event ignored"}

	device_serial = payload.get("shortSerialNumber") or "UNKNOWN"
	serial_no = event_data.get("serialNo") or frappe.utils.generate_hash(length=10)

	event_name = f"HIK-EV-{device_serial}-{serial_no}"
	if frappe.db.exists("Hikvision Event", event_name):
		return {"status": "received", "message": "Duplicate event ignored"}

	# Burst protection: Check for same employee, same device, within last 5 seconds
	employee_no = event_data.get("employeeNoString")

	if device_serial and employee_no:
		recent_event = frappe.db.sql("""
			SELECT name FROM `tabHikvision Event`
			WHERE device_serial = %s AND employee_no = %s
			AND creation > NOW() - INTERVAL 5 SECOND
			LIMIT 1
		""", (device_serial, employee_no))

		if recent_event:
			return {"status": "received", "message": "Burst event ignored"}

	try:
		doc = create_hikvision_event(payload)
		process_event(doc, image_file=image_file)
	except Exception as e:
		frappe.log_error(title=_("Hikvision Webhook Error"), message=frappe.get_traceback())
		return {"status": "error", "message": str(e)}

	return {"status": "received"}


def create_hikvision_event(payload):
	event_data = payload.get("AccessControllerEvent", {})
	device_serial = payload.get("shortSerialNumber")

	device = None
	if device_serial:
		if not frappe.db.exists("Hikvision Device", device_serial):
			device_doc = frappe.get_doc({
				"doctype": "Hikvision Device",
				"device_serial": device_serial,
				"device_name": event_data.get("deviceName") or f"Device {device_serial}",
				"ip_address": payload.get("ipAddress")
			})
			device_doc.insert(ignore_permissions=True)
			frappe.db.commit()

		device = device_serial
		frappe.db.set_value("Hikvision Device", device_serial, "last_heartbeat", frappe.utils.now_datetime())

	event_time = payload.get("dateTime")
	if event_time:
		event_time = event_time.replace("T", " ").split("+")[0]
	else:
		event_time = frappe.utils.now_datetime()

	doc = frappe.get_doc({
		"doctype": "Hikvision Event",
		"device_serial": device_serial,
		"device": device,
		"event_serial_no": event_data.get("serialNo"),
		"employee_no": event_data.get("employeeNoString"),
		"employee_name": event_data.get("name"),
		"attendance_status": event_data.get("attendanceStatus"),
		"verify_mode": event_data.get("currentVerifyMode"),
		"event_time": event_time,
		"raw_event": json.dumps(payload, indent=2)
	})

	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


def process_event(event, image_file=None):
	"""
	Process Hikvision Event and create Employee Checkin.
	Returns True if successfully processed, False otherwise.
	"""
	if not event.employee_no:
		# No employee number — nothing to do, mark as skipped so it doesn't loop
		event.db_set("processed", -1)
		return False

	# 1. Find Employee by attendance_device_id (primary, most reliable)
	employee = None
	if frappe.db.exists("DocType", "Employee"):
		employee = frappe.db.get_value("Employee", {"attendance_device_id": event.employee_no}, "name")

	# 2. Fallback: employee_no matches ERPNext Employee ID directly
	if not employee and frappe.db.exists("DocType", "Employee") and frappe.db.exists("Employee", event.employee_no):
		employee = event.employee_no

	# 3. Fallback: lenient name matching (auto-links on success)
	if not employee and event.employee_name:
		employee = match_employee_by_name(event.employee_name, event.employee_no)

	if not employee:
		frappe.log_error(
			title=_("Hikvision Processing Error"),
			message=_("Employee not found for device ID: {0}").format(event.employee_no)
		)
		# Mark as failed so the batch loop doesn't retry endlessly
		event.db_set("processed", -1)
		return False

	# Update profile picture if image was sent with the event
	if image_file:
		update_employee_image(employee, image_file)

	# Map Hikvision attendanceStatus to ERPNext log_type
	# Hikvision: checkIn, checkOut, breakIn, breakOut, overtimeIn, overtimeOut
	status_map = {
		"checkIn": "IN",
		"checkOut": "OUT",
		"breakIn": "IN",
		"breakOut": "OUT",
		"overtimeIn": "IN",
		"overtimeOut": "OUT"
	}
	log_type = status_map.get(event.attendance_status, "IN")

	try:
		if not frappe.db.exists("DocType", "Employee Checkin"):
			frappe.log_error(
				title=_("Hikvision Processing Error"),
				message=_("Employee Checkin DocType not found. Is ERPNext HR installed?")
			)
			return False

		checkin_data = {
			"doctype": "Employee Checkin",
			"employee": employee,
			"time": event.event_time,
			"log_type": log_type,
			"device_id": event.device_serial
		}

		# Only add attendance_device_id if it's a known field (it may be custom)
		meta = frappe.get_meta("Employee Checkin")
		if meta.has_field("attendance_device_id"):
			checkin_data["attendance_device_id"] = event.employee_no

		# Do NOT set latitude/longitude to 0.0 — doing so triggers HRMS's
		# validate_distance_from_shift_location even when no shift location is configured.
		# Leave them absent so the validator is not invoked.

		checkin = frappe.get_doc(checkin_data)
		checkin.insert(ignore_permissions=True)

		event.db_set("processed", 1)
		frappe.db.commit()
		return True

	except Exception as e:
		frappe.log_error(
			title=_("Hikvision Checkin Error"),
			message=f"Employee: {employee}\nError: {str(e)}\n\nTraceback: {frappe.get_traceback()}"
		)
		event.db_set("processed", -1)
		frappe.db.commit()
		return False


def match_employee_by_name(employee_name, device_id):
	"""
	Leniently match an employee by name and update their attendance_device_id.
	Matches by lowercase and handles reversed name parts (First Last vs Last First).
	"""
	if not frappe.db.exists("DocType", "Employee"):
		return None

	search_name = employee_name.strip().lower()
	if not search_name:
		return None

	employees = frappe.db.get_all("Employee", filters={"status": "Active"}, fields=["name", "employee_name"])

	matches = []
	search_parts = set(search_name.split())

	for emp in employees:
		if not emp.employee_name:
			continue

		emp_name_lower = emp.employee_name.strip().lower()
		emp_parts = set(emp_name_lower.split())

		# Exact match
		if search_name == emp_name_lower:
			matches.append(emp.name)
			continue

		# Match parts regardless of order (e.g., "John Doe" vs "Doe John")
		if search_parts == emp_parts and len(search_parts) > 1:
			matches.append(emp.name)

	if len(matches) == 1:
		matched_emp = matches[0]
		try:
			if frappe.get_meta("Employee").has_field("attendance_device_id"):
				frappe.db.set_value("Employee", matched_emp, "attendance_device_id", device_id)
				frappe.db.commit()
				frappe.log_error(
					title=_("Hikvision Auto-Link"),
					message=_("Automatically linked employee {0} ({1}) to device ID {2}").format(matched_emp, employee_name, device_id)
				)
				return matched_emp
		except Exception as e:
			frappe.log_error(title=_("Hikvision Auto-Link Error"), message=str(e))

	return None


def update_employee_image(employee_name, image_file):
	"""
	Update the profile picture of an employee using the image from the Hikvision terminal.
	Always overwrites the existing image so the photo stays current with the terminal's record.
	"""
	try:
		from frappe.utils.file_manager import save_file

		content = image_file.read()
		if not content:
			return

		if isinstance(content, str):
			content = content.encode("utf-8")

		file_name = f"hik_{employee_name}_{frappe.utils.generate_hash(length=6)}.jpg"

		# Delete existing Hikvision-sourced profile photo to avoid accumulating files
		existing_files = frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "Employee",
				"attached_to_name": employee_name,
				"file_name": ["like", "hik_%"]
			},
			fields=["name"]
		)
		for f in existing_files:
			try:
				frappe.delete_doc("File", f.name, ignore_permissions=True)
			except Exception:
				pass

		saved_file = save_file(
			file_name,
			content,
			"Employee",
			employee_name,
			decode=False,
			is_private=0
		)

		if frappe.get_meta("Employee").has_field("image"):
			frappe.db.set_value("Employee", employee_name, "image", saved_file.file_url)

		frappe.db.commit()

		# Reset file pointer in case it's needed again upstream
		image_file.seek(0)

	except Exception as e:
		frappe.log_error(
			title=_("Hikvision Image Update Error"),
			message=f"Employee: {employee_name}\nError: {str(e)}\n\nTraceback: {frappe.get_traceback()}"
		)


@frappe.whitelist()
def enqueue_process_unprocessed_events():
	"""
	Whitelisted method to manually trigger background processing of unprocessed events.
	Call via: /api/method/hikvision_integration.api.enqueue_process_unprocessed_events
	"""
	frappe.enqueue(
		"hikvision_integration.api.process_unprocessed_events",
		queue="long",
		timeout=3600
	)
	return {"status": "enqueued", "message": _("Processing of unprocessed events has been enqueued.")}


def process_unprocessed_events(batch_size=100):
	"""
	Background job to process all Hikvision Events not yet converted to Employee Checkins.
	Runs in batches to avoid memory and timeout issues. Processes until none remain.
	"""
	total_processed = 0
	total_failed = 0

	while True:
		unprocessed_events = frappe.get_all(
			"Hikvision Event",
			filters={"processed": 0},
			fields=["name"],
			limit_page_length=batch_size,
			order_by="event_time asc"
		)

		if not unprocessed_events:
			break

		for entry in unprocessed_events:
			event = frappe.get_doc("Hikvision Event", entry.name)
			if process_event(event):
				total_processed += 1
			else:
				total_failed += 1

		frappe.db.commit()

		# If we got fewer than a full batch, there are no more events
		if len(unprocessed_events) < batch_size:
			break

	frappe.log_error(
		title=_("Hikvision Background Processing Complete"),
		message=_("Processed: {0} | Failed: {1}").format(total_processed, total_failed)
	)
