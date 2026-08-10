import frappe

def execute():
	"""Fix corrupted filters_json in tabNumber Card where operator was saved as 'Today' instead of 'Timespan'."""
	frappe.db.sql("""
		UPDATE `tabNumber Card`
		SET filters_json = '[["Hikvision Event","event_time","Timespan","today"]]'
		WHERE name = 'Hikvision Events Today'
	""")
	frappe.db.commit()
