import json
import frappe
from frappe import _

@frappe.whitelist(allow_guest=True)
def webhook():
	payload = None

	# 1. Try to get JSON from request (standard JSON body)
	try:
		payload = frappe.request.get_json()
	except Exception:
		pass

	# 2. Try to get from multipart form data (Hikvision standard with images)
	if not payload:
		try:
			# Hikvision sends JSON in a form field named 'AccessControllerEvent'
			event_json = frappe.request.form.get("AccessControllerEvent")
			if event_json:
				payload = json.loads(event_json)
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
						if 'name="AccessControllerEvent"' in part:
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
		# Log that we failed to get a payload
		frappe.log_error(
			title=_("Hikvision Webhook No Payload"),
			message=f"Headers: {frappe.request.headers}\nForm Keys: {list(frappe.request.form.keys()) if frappe.request.form else 'None'}\nData Preview: {frappe.request.get_data(as_text=True)[:500]}"
		)
		return {"status": "error", "message": "No payload received"}

	# Log the raw payload for debugging in Error Log so it's visible in Desk
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
	device_serial = payload.get("shortSerialNumber") or "UNKNOWN"
	serial_no = event_data.get("serialNo") or frappe.utils.generate_hash(length=10)

	event_name = f"HIK-EV-{device_serial}-{serial_no}"
	if frappe.db.exists("Hikvision Event", event_name):
		# We check if it's processed. If not, maybe we should try processing again?
		# But usually, it means it's already in the system.
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
		process_event(doc)
	except Exception as e:
		frappe.log_error(title=_("Hikvision Webhook Error"), message=frappe.get_traceback())
		return {"status": "error", "message": str(e)}

	return {"status": "received"}

def create_hikvision_event(payload):
	event_data = payload.get("AccessControllerEvent", {})
	device_serial = payload.get("shortSerialNumber")

	# Link to Hikvision Device if it exists, otherwise create it
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
		# Update last heartbeat
		frappe.db.set_value("Hikvision Device", device_serial, "last_heartbeat", frappe.utils.now_datetime())

	doc = frappe.get_doc({
		"doctype": "Hikvision Event",
		"device_serial": device_serial,
		"device": device,
		"event_serial_no": event_data.get("serialNo"),
		"employee_no": event_data.get("employeeNoString"),
		"employee_name": event_data.get("name"),
		"attendance_status": event_data.get("attendanceStatus"),
		"verify_mode": event_data.get("currentVerifyMode"),
		"event_time": payload.get("dateTime").replace("T", " ").split("+")[0],
		"raw_event": json.dumps(payload, indent=2)
	})

	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc

def process_event(event):
	"""
	Process Hikvision Event and create Employee Checkin
	"""
	if not event.employee_no:
		return

	# Find Employee by attendance_device_id
	employee = None
	if frappe.db.exists("DocType", "Employee"):
		employee = frappe.db.get_value("Employee", {"attendance_device_id": event.employee_no}, "name")

	if not employee and event.employee_name:
		# Try lenient name matching to ease setup
		employee = match_employee_by_name(event.employee_name, event.employee_no)

	if not employee:
		# Fallback to employee_no if it matches ERPNext Employee ID (optional, but good for testing)
		if frappe.db.exists("DocType", "Employee") and frappe.db.exists("Employee", event.employee_no):
			employee = event.employee_no
		else:
			frappe.log_error(title=_("Hikvision Processing Error"), message=_("Employee not found for device ID: {0}").format(event.employee_no))
			return

	# Map attendanceStatus to log_type
	# Hikvision uses checkIn/checkOut, ERPNext uses IN/OUT
	# Possible Hikvision values: checkIn, checkOut, breakIn, breakOut, overtimeIn, overtimeOut
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
			return

		checkin_data = {
			"doctype": "Employee Checkin",
			"employee": employee,
			"time": event.event_time,
			"log_type": log_type,
			"device_id": event.device_serial
		}

		# Handle mandatory geolocation fields if they exist (ERPNext feature)
		meta = frappe.get_meta("Employee Checkin")
		if meta.has_field("latitude") and meta.get_field("latitude").reqd:
			checkin_data["latitude"] = 0.0
		if meta.has_field("longitude") and meta.get_field("longitude").reqd:
			checkin_data["longitude"] = 0.0

		# Only add attendance_device_id if it's a known field (it might be custom)
		if meta.has_field("attendance_device_id"):
			checkin_data["attendance_device_id"] = event.employee_no

		checkin = frappe.get_doc(checkin_data)
		checkin.insert(ignore_permissions=True)

		# Log success for visibility
		frappe.log_error(
			title=_("Hikvision Checkin Success"),
			message=_("Created Employee Checkin for {0} at {1} ({2})").format(employee, event.event_time, log_type)
		)

		# Mark event as processed
		event.processed = 1
		event.save()
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(
			title=_("Hikvision Checkin Error"),
			message=f"Error: {str(e)}\n\nTraceback: {frappe.get_traceback()}"
		)

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

	# Get all active employees with their names
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

	# If exactly one match found, update the employee's attendance_device_id
	if len(matches) == 1:
		matched_emp = matches[0]
		try:
			# Verify if attendance_device_id field exists
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

@frappe.whitelist()
def enqueue_process_unprocessed_events():
	"""
	Whitelisted method to manually trigger background processing of events.
	"""
	frappe.enqueue("hikvision_integration.api.process_unprocessed_events", queue="long", timeout=600)
	return {"status": "enqueued", "message": _("Processing of unprocessed events has been enqueued.")}

def process_unprocessed_events(batch_size=100):
	"""
	Background job to process Hikvision Events that haven't been converted to Employee Checkins.
	Processes in batches to avoid timeouts and high resource usage.
	"""
	# Find unprocessed events, ordered by event_time (oldest first)
	unprocessed_events = frappe.get_all(
		"Hikvision Event",
		filters={"processed": 0},
		fields=["name"],
		limit_page_length=batch_size,
		order_by="event_time asc"
	)

	if not unprocessed_events:
		return

	count = 0
	for entry in unprocessed_events:
		event = frappe.get_doc("Hikvision Event", entry.name)
		try:
			# process_event handles employee lookup and checkin creation
			process_event(event)
			count += 1
		except Exception:
			# Individual failures are logged inside process_event,
			# we continue with the rest of the batch
			continue

	if count > 0:
		frappe.log_error(
			title=_("Hikvision Background Processing"),
			message=_("Processed {0} previously unprocessed events.").format(count)
		)
