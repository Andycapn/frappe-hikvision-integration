import frappe

def execute():
	"""Fix corrupted filters_json in tabNumber Card and force reload Workspace links."""
	frappe.db.sql("""
		UPDATE `tabNumber Card`
		SET filters_json = '[["Hikvision Event","event_time","Timespan","today"]]'
		WHERE name = 'Hikvision Events Today'
	""")
	frappe.db.commit()

	try:
		frappe.reload_doc("workspace", "hikvision_integration", "hikvision_integration", force=True)
	except Exception:
		pass
