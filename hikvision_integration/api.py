import json
import frappe
from frappe import _

# Hard cap on incoming webhook body to prevent memory exhaustion
_MAX_PAYLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


# ---------------------------------------------------------------------------
# Webhook authentication
# ---------------------------------------------------------------------------

def _verify_webhook_request():
	"""
	Reject requests that don't come from a known device IP.

	Configure in site_config.json:
	  "hikvision_allowed_ips": ["192.168.1.10", "192.168.1.11"]

	If the key is absent or empty the check is skipped (backwards-compatible),
	but a warning is logged so admins know authentication is unconfigured.
	"""
	allowed_ips = frappe.conf.get("hikvision_allowed_ips")

	if not allowed_ips:
		# Warn once per process restart — don't flood the log
		if not getattr(frappe.local, "_hik_auth_warned", False):
			frappe.log_error(
				title=_("Hikvision Webhook: No IP Allowlist Configured"),
				message=_(
					"The webhook endpoint is open to any IP address. "
					"Set 'hikvision_allowed_ips' in site_config.json to restrict access."
				)
			)
			frappe.local._hik_auth_warned = True
		return  # allow through but warned

	remote_addr = frappe.request.environ.get("HTTP_X_FORWARDED_FOR") or \
				  frappe.request.environ.get("REMOTE_ADDR", "")
	# X-Forwarded-For may be a comma-separated list; take the first (client) IP
	client_ip = remote_addr.split(",")[0].strip()

	if client_ip not in allowed_ips:
		frappe.throw(
			_("Webhook request from unauthorized IP: {0}").format(client_ip),
			frappe.PermissionError
		)


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def _parse_payload():
	"""
	Try every format Hikvision devices use. Returns (payload_dict, image_file_or_None).
	Returns (None, None) if nothing could be parsed.
	"""
	# Guard against oversized bodies early
	content_length = frappe.request.content_length or 0
	if content_length > _MAX_PAYLOAD_BYTES:
		frappe.throw(_("Webhook payload too large ({0} bytes)").format(content_length))

	image_file = None

	# 1. Standard JSON body
	try:
		payload = frappe.request.get_json(silent=True)
		if payload:
			return payload, None
	except Exception:
		pass

	# 2. Multipart form data (most Hikvision models with images)
	try:
		for field in ("AccessControllerEvent", "event_log"):
			event_json = frappe.request.form.get(field)
			if event_json:
				payload = json.loads(event_json)
				if "Picture" in frappe.request.files:
					image_file = frappe.request.files["Picture"]
				return payload, image_file
	except Exception:
		pass

	# 3. Raw body fallback — some firmware sends malformed multipart
	try:
		raw = frappe.request.get_data()
		if not raw:
			return None, None

		if len(raw) > _MAX_PAYLOAD_BYTES:
			frappe.throw(_("Webhook payload too large"))

		text = raw.decode("utf-8", errors="replace")

		if "--MIME_boundary" in text:
			parts = text.split("--MIME_boundary")
			for part in parts:
				if 'name="AccessControllerEvent"' in part or 'name="event_log"' in part:
					start = part.find("{")
					end = part.rfind("}")
					if start != -1 and end != -1:
						return json.loads(part[start:end + 1]), None
		else:
			return json.loads(text), None
	except Exception:
		pass

	return None, None


# ---------------------------------------------------------------------------
# Public webhook endpoint
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def webhook():
	_verify_webhook_request()

	payload, image_file = _parse_payload()

	if not payload:
		frappe.log_error(
			title=_("Hikvision Webhook: No Payload"),
			message=(
				f"Headers: {dict(frappe.request.headers)}\n"
				f"Form keys: {list(frappe.request.form.keys()) if frappe.request.form else 'None'}"
			)
		)
		return {"status": "error", "message": "No payload received"}

	# Heartbeat — keep alive, no employee data involved
	if payload.get("eventType") == "heartBeat":
		return _handle_heartbeat(payload)

	event_data = payload.get("AccessControllerEvent", {})
	device_serial = payload.get("shortSerialNumber") or "UNKNOWN"
	attendance_status = event_data.get("attendanceStatus")

	# Determine device role (default to Attendance Gate for unknown devices)
	device_role = (
		frappe.db.get_value("Hikvision Device", device_serial, "device_role")
		or "Attendance Gate"
	)

	# Presence terminals: skip entirely if there is no employee number
	# (pure door-open events with no identity claim are noise)
	if not event_data.get("employeeNoString"):
		return {"status": "received", "message": "No employee identity — event ignored"}

	# Attendance gates: drop undefined-status events (plain door open, not a checkin)
	# Presence terminals: allow undefined-status through — stored as presence-only
	if device_role == "Attendance Gate" and attendance_status in ("undefined", None):
		return {"status": "received", "message": "Non-attendance event ignored"}

	serial_no = event_data.get("serialNo") or frappe.utils.generate_hash(length=10)
	event_name = f"HIK-EV-{device_serial}-{serial_no}"

	if frappe.db.exists("Hikvision Event", event_name):
		return {"status": "received", "message": "Duplicate event ignored"}

	# Burst protection: same employee + device within 5 seconds
	employee_no = event_data.get("employeeNoString")
	if device_serial and employee_no:
		recent = frappe.db.sql(
			"""
			SELECT name FROM `tabHikvision Event`
			WHERE device_serial = %s AND employee_no = %s
			  AND creation > NOW() - INTERVAL 5 SECOND
			LIMIT 1
			""",
			(device_serial, employee_no),
		)
		if recent:
			return {"status": "received", "message": "Burst event ignored"}

	try:
		doc = _create_hikvision_event(payload)
		process_event(doc, image_file=image_file, device_role=device_role)
	except Exception:
		frappe.log_error(title=_("Hikvision Webhook Error"), message=frappe.get_traceback())
		return {"status": "error", "message": "Internal error — see Error Log"}

	return {"status": "received"}


# ---------------------------------------------------------------------------
# Heartbeat handler
# ---------------------------------------------------------------------------

def _infer_device_role(device_name):
	"""
	Best-effort role inference from the device name string sent by the hardware.
	DS-K1T673 series → Attendance Gate; DS-K1T808 series → Presence Terminal.
	Admins should confirm/correct this in the Hikvision Device list after first contact.
	"""
	name_upper = (device_name or "").upper()
	if "K1T808" in name_upper:
		return "Presence Terminal"
	return "Attendance Gate"


def _handle_heartbeat(payload):
	device_serial = payload.get("shortSerialNumber")
	if not device_serial:
		return {"status": "received", "message": "Heartbeat (no serial)"}

	if not frappe.db.exists("Hikvision Device", device_serial):
		device_name = payload.get("deviceName") or f"Device {device_serial}"
		frappe.get_doc({
			"doctype": "Hikvision Device",
			"device_serial": device_serial,
			"device_name": device_name,
			"ip_address": payload.get("ipAddress"),
			"device_role": _infer_device_role(device_name),
		}).insert(ignore_permissions=True)

	frappe.db.set_value("Hikvision Device", device_serial, "last_heartbeat", frappe.utils.now_datetime())
	frappe.db.commit()
	return {"status": "received", "message": "Heartbeat updated"}


# ---------------------------------------------------------------------------
# Event creation
# ---------------------------------------------------------------------------

def _create_hikvision_event(payload):
	event_data = payload.get("AccessControllerEvent", {})
	device_serial = payload.get("shortSerialNumber")

	if device_serial:
		if not frappe.db.exists("Hikvision Device", device_serial):
			# Infer role from model prefix in serial or device name when auto-creating.
			# Admins should review and correct this in the Hikvision Device list.
			inferred_role = _infer_device_role(event_data.get("deviceName") or "")
			frappe.get_doc({
				"doctype": "Hikvision Device",
				"device_serial": device_serial,
				"device_name": event_data.get("deviceName") or f"Device {device_serial}",
				"ip_address": payload.get("ipAddress"),
				"device_role": inferred_role,
			}).insert(ignore_permissions=True)

		frappe.db.set_value("Hikvision Device", device_serial, "last_heartbeat", frappe.utils.now_datetime())

	event_time = payload.get("dateTime")
	if event_time:
		event_time = event_time.replace("T", " ").split("+")[0]
	else:
		event_time = frappe.utils.now_datetime()

	doc = frappe.get_doc({
		"doctype": "Hikvision Event",
		"device_serial": device_serial,
		"device": device_serial,
		"event_serial_no": event_data.get("serialNo"),
		"employee_no": event_data.get("employeeNoString"),
		"employee_name": event_data.get("name"),
		"attendance_status": event_data.get("attendanceStatus"),
		"verify_mode": event_data.get("currentVerifyMode"),
		"event_time": event_time,
		"raw_event": json.dumps(payload, indent=2),
	})

	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------

_CHECKIN_STATUSES = {"checkIn", "checkOut", "breakIn", "breakOut", "overtimeIn", "overtimeOut"}


def process_event(event, image_file=None, device_role=None):
	"""
	Process a Hikvision Event and create an Employee Checkin if appropriate.

	device_role is passed in from the webhook to avoid an extra DB query.
	When called from the background reprocessing job it is looked up fresh.

	processed values:
	  0  = pending
	  1  = converted to Employee Checkin
	  2  = presence-only (stored, no checkin needed)
	 -1  = failed or skipped
	"""
	if not event.employee_no:
		event.db_set("processed", -1)
		return False

	if not frappe.db.exists("DocType", "Employee"):
		event.db_set("processed", -1)
		return False

	# Resolve device role if not supplied (background job path)
	if device_role is None:
		device_role = (
			frappe.db.get_value("Hikvision Device", event.device_serial, "device_role")
			or "Attendance Gate"
		)

	# Presence terminals with no explicit attendance status → presence-only, no checkin
	if device_role == "Presence Terminal" and event.attendance_status not in _CHECKIN_STATUSES:
		event.db_set("processed", 2)
		return True  # successfully handled, just not a checkin

	employee = _resolve_employee(event)

	if not employee:
		frappe.log_error(
			title=_("Hikvision: Employee Not Found"),
			message=_("No employee matched device ID: {0}").format(event.employee_no)
		)
		event.db_set("processed", -1)
		return False

	if image_file:
		_update_employee_image(employee, image_file)

	status_map = {
		"checkIn": "IN",
		"checkOut": "OUT",
		"breakIn": "IN",
		"breakOut": "OUT",
		"overtimeIn": "IN",
		"overtimeOut": "OUT",
	}
	log_type = status_map.get(event.attendance_status, "IN")

	try:
		if not frappe.db.exists("DocType", "Employee Checkin"):
			frappe.log_error(
				title=_("Hikvision: DocType Missing"),
				message=_("Employee Checkin DocType not found. Is ERPNext HR installed?")
			)
			return False

		checkin_data = {
			"doctype": "Employee Checkin",
			"employee": employee,
			"time": event.event_time,
			"log_type": log_type,
			"device_id": event.device_serial,
		}

		meta = frappe.get_meta("Employee Checkin")
		if meta.has_field("attendance_device_id"):
			checkin_data["attendance_device_id"] = event.employee_no

		# Do NOT set latitude/longitude — triggers HRMS shift-location validation
		# even when no shift location is configured.

		frappe.get_doc(checkin_data).insert(ignore_permissions=True)
		event.db_set("processed", 1)
		frappe.db.commit()
		return True

	except Exception:
		frappe.log_error(
			title=_("Hikvision Checkin Error"),
			message=f"Employee: {employee}\n\n{frappe.get_traceback()}"
		)
		event.db_set("processed", -1)
		frappe.db.commit()
		return False


def _resolve_employee(event):
	"""Return the ERPNext Employee name for this event, or None."""
	# 1. attendance_device_id (canonical)
	employee = frappe.db.get_value(
		"Employee", {"attendance_device_id": event.employee_no}, "name"
	)
	if employee:
		return employee

	# 2. Employee ID directly
	if frappe.db.exists("Employee", event.employee_no):
		return event.employee_no

	# 3. Name-based fallback — only if explicitly enabled
	if frappe.conf.get("hikvision_autolink_by_name") and event.employee_name:
		return _match_employee_by_name(event.employee_name, event.employee_no)

	return None


def _match_employee_by_name(employee_name, device_id):
	"""
	Leniently match an employee by name and update their attendance_device_id.

	This is disabled by default because a device-supplied name could
	accidentally (or maliciously) re-link an employee. Enable with:
	  "hikvision_autolink_by_name": true  in site_config.json
	"""
	search_name = employee_name.strip().lower()
	if not search_name:
		return None

	employees = frappe.db.get_all(
		"Employee",
		filters={"status": "Active"},
		fields=["name", "employee_name"],
	)

	search_parts = set(search_name.split())
	matches = []

	for emp in employees:
		if not emp.employee_name:
			continue
		emp_lower = emp.employee_name.strip().lower()
		emp_parts = set(emp_lower.split())

		if search_name == emp_lower:
			matches.append(emp.name)
		elif search_parts == emp_parts and len(search_parts) > 1:
			matches.append(emp.name)

	if len(matches) != 1:
		return None  # ambiguous or no match — don't guess

	matched = matches[0]
	try:
		if frappe.get_meta("Employee").has_field("attendance_device_id"):
			frappe.db.set_value("Employee", matched, "attendance_device_id", device_id)
			frappe.db.commit()
			frappe.log_error(
				title=_("Hikvision Auto-Link"),
				message=_("Linked employee {0} to device ID {1} via name match").format(
					matched, device_id
				)
			)
		return matched
	except Exception:
		frappe.log_error(title=_("Hikvision Auto-Link Error"), message=frappe.get_traceback())
		return None


# ---------------------------------------------------------------------------
# Employee image update
# ---------------------------------------------------------------------------

def _update_employee_image(employee_name, image_file):
	"""
	Replace an employee's profile picture with the photo from the Hikvision terminal.
	Stored as private so it is not publicly accessible.
	"""
	try:
		from frappe.utils.file_manager import save_file

		content = image_file.read()
		if not content:
			return

		if isinstance(content, str):
			content = content.encode("utf-8")

		file_name = f"hik_{employee_name}_{frappe.utils.generate_hash(length=6)}.jpg"

		# Remove previous Hikvision-sourced photos to avoid accumulation
		old_files = frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "Employee",
				"attached_to_name": employee_name,
				"file_name": ["like", "hik_%"],
			},
			fields=["name"],
		)
		for f in old_files:
			try:
				frappe.delete_doc("File", f.name, ignore_permissions=True)
			except Exception:
				pass

		saved = save_file(
			file_name,
			content,
			"Employee",
			employee_name,
			decode=False,
			is_private=1,  # employee photos must not be publicly accessible
		)

		if frappe.get_meta("Employee").has_field("image"):
			frappe.db.set_value("Employee", employee_name, "image", saved.file_url)

		frappe.db.commit()
		image_file.seek(0)

	except Exception:
		frappe.log_error(
			title=_("Hikvision Image Update Error"),
			message=f"Employee: {employee_name}\n\n{frappe.get_traceback()}"
		)


# ---------------------------------------------------------------------------
# Background reprocessing
# ---------------------------------------------------------------------------

@frappe.whitelist()
def enqueue_process_unprocessed_events():
	"""
	Manually trigger background processing of unprocessed events.
	Call via: /api/method/hikvision_integration.api.enqueue_process_unprocessed_events
	"""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"hikvision_integration.api.process_unprocessed_events",
		queue="long",
		timeout=3600,
	)
	return {"status": "enqueued", "message": _("Processing of unprocessed events has been enqueued.")}


def process_unprocessed_events(batch_size=100):
	"""
	Background job: convert pending Hikvision Events to Employee Checkins.
	Runs in batches to avoid memory/timeout issues.
	"""
	total_processed = 0
	total_failed = 0

	while True:
		unprocessed = frappe.get_all(
			"Hikvision Event",
			filters={"processed": 0},
			fields=["name"],
			limit_page_length=batch_size,
			order_by="event_time asc",
		)

		if not unprocessed:
			break

		for entry in unprocessed:
			event = frappe.get_doc("Hikvision Event", entry.name)
			if process_event(event):
				total_processed += 1
			else:
				total_failed += 1

		frappe.db.commit()

		if len(unprocessed) < batch_size:
			break

	frappe.log_error(
		title=_("Hikvision Background Processing Complete"),
		message=_("Processed: {0} | Failed/Skipped: {1}").format(total_processed, total_failed)
	)
