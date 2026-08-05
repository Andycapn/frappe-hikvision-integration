import csv
import datetime
import os

def generate_shift_assignments():
    checkins_path = "/Users/andrewndhlovu/Downloads/Employee Checkin(1).csv"
    emp_path = "/Users/andrewndhlovu/Downloads/Employee(12).csv"
    output_path = "/Users/andrewndhlovu/Downloads/Shift_Assignments_Import.csv"
    
    # 1. Load Employees with their details
    employees = {}
    try:
        with open(emp_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            headers = []
            start_reading = False
            for row in reader:
                if len(row) > 0 and row[0] == "Column Name:":
                    headers = [h.strip() for h in row]
                if len(row) > 0 and row[0] == "Start entering data below this line":
                    start_reading = True
                    continue
                if start_reading and len(row) > 0:
                    try:
                        emp_id = row[headers.index("name")].strip('"').strip("'").strip()
                        dept = row[headers.index("department")].strip('"').strip("'").strip()
                        status = row[headers.index("status")].strip('"').strip("'").strip()
                        
                        if status == "Active":
                            employees[emp_id] = {
                                "department": dept,
                                "punches": []
                            }
                    except ValueError:
                        pass
        print(f"Loaded {len(employees)} active employees.")
    except Exception as e:
        print(f"Failed to load employees: {e}")
        return

    # 2. Load Checkins and associate with employees
    total_punches = 0
    with open(checkins_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        headers = []
        start_reading = False
        for row in reader:
            if len(row) > 0 and row[0] == "Column Name:":
                headers = [h.strip() for h in row]
            if len(row) > 0 and row[0] == "Start entering data below this line":
                start_reading = True
                continue
            if start_reading and len(row) > 0:
                try:
                    emp_idx = headers.index("employee")
                    time_idx = headers.index("time")
                except ValueError:
                    continue
                    
                if len(row) > max(emp_idx, time_idx):
                    emp = row[emp_idx].strip('"').strip("'").strip()
                    time_str = row[time_idx]
                    
                    if not time_str or emp not in employees:
                        continue
                    
                    try:
                        dt = datetime.datetime.strptime(time_str.strip(), "%d-%m-%Y %H:%M:%S")
                    except ValueError:
                        try:
                            dt = datetime.datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            continue
                            
                    employees[emp]["punches"].append(dt)
                    total_punches += 1

    print(f"Linked {total_punches} check-ins to active employees.")

    # 3. Determine shifts individually
    assignments = []
    
    # We define non-overlapping operational shifts for rotating staff:
    # 1. Operations Morning Shift: 06:00 - 14:00
    # 2. Operations Afternoon Shift: 14:00 - 22:00
    # 3. Operations Night Shift: 22:00 - 06:00
    # These have 0 overlap, so they can be assigned concurrently!
    SHIFTS = {
        "admin_day": "Standard Day Shift",           # 07:30 - 17:00 (For Admin/Management/Game only)
        "ops_morning": "Operations Morning Shift",   # 06:00 - 14:00 (Rotating Morning)
        "ops_afternoon": "Operations Afternoon Shift", # 14:00 - 22:00 (Rotating Afternoon)
        "ops_night": "Operations Night Shift",       # 22:00 - 06:00 (Rotating Night)
        "sec_day": "Security Day Shift",             # 06:00 - 18:00 (Security Day)
        "sec_night": "Security Night Shift"          # 18:00 - 06:00 (Security Night)
    }

    for emp_id, info in employees.items():
        dept = info["department"]
        punches = info["punches"]
        assigned_shifts = set()

        if not punches:
            # Fallbacks
            if "Security" in dept:
                assigned_shifts.add(SHIFTS["sec_day"])
            elif any(d in dept for d in ["Food & Beverages", "Kitchen", "Front Office", "Stores"]):
                assigned_shifts.add(SHIFTS["ops_morning"])
            else:
                assigned_shifts.add(SHIFTS["admin_day"])
        else:
            hours = [p.hour for p in punches]
            
            # Count punches in different windows
            night_punches = sum(1 for h in hours if h >= 22 or h < 6)
            morning_punches = sum(1 for h in hours if 6 <= h < 14)
            afternoon_punches = sum(1 for h in hours if 14 <= h < 22)

            if "Security" in dept:
                # Security has 12-hour shifts (06:00 - 18:00 and 18:00 - 06:00) - No overlap!
                if any(h for h in hours if 6 <= h < 18):
                    assigned_shifts.add(SHIFTS["sec_day"])
                if any(h for h in hours if h >= 18 or h < 6):
                    assigned_shifts.add(SHIFTS["sec_night"])
                if not assigned_shifts:
                    assigned_shifts.add(SHIFTS["sec_day"])
            elif any(d in dept for d in ["Food & Beverages", "Kitchen", "Front Office", "Stores"]):
                # Rotating departments get assigned to non-overlapping Ops Shifts:
                if morning_punches > 0:
                    assigned_shifts.add(SHIFTS["ops_morning"])
                if afternoon_punches > 0:
                    assigned_shifts.add(SHIFTS["ops_afternoon"])
                if night_punches > 0:
                    assigned_shifts.add(SHIFTS["ops_night"])
                if not assigned_shifts:
                    assigned_shifts.add(SHIFTS["ops_morning"])
            else:
                # Regular non-rotating staff gets Standard Day Shift
                assigned_shifts.add(SHIFTS["admin_day"])

        for shift in assigned_shifts:
            assignments.append({
                "employee": emp_id,
                "shift_type": shift,
                "start_date": "01-06-2026",
                "end_date": "31-12-2026",
                "status": "Active"
            })

    # 4. Write to plain CSV format
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["employee", "shift_type", "start_date", "end_date", "status"])
        
        for assign in assignments:
            writer.writerow([
                assign["employee"],
                assign["shift_type"],
                assign["start_date"],
                assign["end_date"],
                assign["status"]
            ])

    print(f"Successfully generated {len(assignments)} Shift Assignments in {output_path}")

if __name__ == "__main__":
    generate_shift_assignments()
