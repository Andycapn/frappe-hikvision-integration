frappe.pages['attendance-control-center'].on_page_load = function(wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Attendance & Payroll Control Center 🛡️',
		single_column: true
	});

	page.set_secondary_action('Refresh', function() {
		load_dashboard(page);
	}, 'octicon octicon-sync');

	page.set_primary_action('Run Axon Attendance Sync ⚡', function() {
		frappe.call({
			method: 'hikvision_integration.axon_engine.process_axon_attendance',
			freeze: true,
			freeze_message: 'Running Axon Attendance Engine...',
			callback: function(r) {
				frappe.msgprint(r.message || 'Attendance sync completed successfully.');
				load_dashboard(page);
			}
		});
	});

	load_dashboard(page);
};

function load_dashboard(page) {
	let $body = $(page.body).empty();

	// Load KPIs
	frappe.call({
		method: 'hikvision_integration.axon_engine.get_control_center_kpis',
		callback: function(r) {
			let kpis = r.message || { today_swipes: 0, pending_anomalies: 0, approved_anomalies: 0, readiness_status: 'READY' };
			render_kpis($body, kpis);
			render_anomalies_table($body, page);
		}
	});
}

function render_kpis($container, kpis) {
	let lock_badge = kpis.pending_anomalies > 0 
		? `<span class="axon-badge axon-badge-locked">🔒 PAYROLL LOCKED</span>`
		: `<span class="axon-badge axon-badge-ready">🔓 READY FOR PAYROLL</span>`;

	let html = `
		<div class="axon-dashboard-container">
			<div class="axon-kpi-grid">
				<div class="axon-kpi-card">
					<div class="axon-kpi-header">
						<span class="axon-kpi-title">Today's Swipes</span>
						<span class="axon-kpi-icon">📡</span>
					</div>
					<div class="axon-kpi-val">${kpis.today_swipes}</div>
				</div>

				<div class="axon-kpi-card">
					<div class="axon-kpi-header">
						<span class="axon-kpi-title">Pending Anomalies</span>
						<span class="axon-kpi-icon">🟡</span>
					</div>
					<div class="axon-kpi-val">${kpis.pending_anomalies}</div>
				</div>

				<div class="axon-kpi-card">
					<div class="axon-kpi-header">
						<span class="axon-kpi-title">Resolved Anomalies</span>
						<span class="axon-kpi-icon">🟢</span>
					</div>
					<div class="axon-kpi-val">${kpis.approved_anomalies}</div>
				</div>

				<div class="axon-kpi-card">
					<div class="axon-kpi-header">
						<span class="axon-kpi-title">Payroll Status</span>
						<span class="axon-kpi-icon">💼</span>
					</div>
					<div style="margin-top: 6px;">${lock_badge}</div>
				</div>
			</div>
		</div>
	`;
	$container.append(html);
}

function render_anomalies_table($container, page) {
	frappe.call({
		method: 'hikvision_integration.axon_engine.get_anomalies_for_review',
		callback: function(r) {
			let list = r.message || [];
			let $section = $(`
				<div class="axon-dashboard-container">
					<div class="axon-section">
						<div class="axon-section-header">
							<div class="axon-section-title">
								<span>🛡️ Pending Attendance Anomalies Review (${list.length})</span>
							</div>
							${list.length > 0 ? `
								<button class="axon-btn axon-btn-remote" id="btn-batch-remote">
									⚡ Approve All Selected as Remote Duty (8h)
								</button>
							` : ''}
						</div>
						<div class="axon-table-wrapper" id="anomalies-table-container"></div>
					</div>
				</div>
			`);

			$container.append($section);

			if (list.length === 0) {
				$section.find('#anomalies-table-container').html(`
					<div style="text-align: center; padding: 40px; color: var(--text-muted);">
						<div style="font-size: 32px; margin-bottom: 8px;">🎉</div>
						<div style="font-size: 16px; font-weight: 600;">No Pending Anomalies!</div>
						<div style="font-size: 13px; margin-top: 4px;">All check-in logs are fully processed and payroll is unlocked.</div>
					</div>
				`);
				return;
			}

			let tableHtml = `
				<table class="axon-table">
					<thead>
						<tr>
							<th style="width: 30px;"><input type="checkbox" id="chk-select-all"></th>
							<th>Employee</th>
							<th>Department</th>
							<th>Date</th>
							<th>Matched Shift</th>
							<th>IN Punch</th>
							<th>Reason</th>
							<th style="text-align: right;">Quick Actions</th>
						</tr>
					</thead>
					<tbody>
			`;

			list.forEach(a => {
				tableHtml += `
					<tr data-name="${a.name}">
						<td><input type="checkbox" class="chk-row" data-name="${a.name}"></td>
						<td><strong>${a.employee_name}</strong><br><small style="color: var(--text-muted);">${a.employee}</small></td>
						<td>${a.department || 'Operations'}</td>
						<td>${a.attendance_date}</td>
						<td><span class="axon-badge axon-badge-pending">${a.matched_shift || 'Operations Shift'}</span></td>
						<td>${a.orphan_in_time ? a.orphan_in_time.split(' ')[1] : '--:--'}</td>
						<td><small style="color: #d97706; font-weight: 600;">${a.anomaly_reason}</small></td>
						<td style="text-align: right;">
							<button class="axon-btn axon-btn-remote btn-action-remote" data-name="${a.name}">🟢 Remote Duty</button>
							<button class="axon-btn axon-btn-absent btn-action-absent" data-name="${a.name}">🔴 Absent</button>
						</td>
					</tr>
				`;
			});

			tableHtml += `</tbody></table>`;
			$section.find('#anomalies-table-container').html(tableHtml);

			// Checkbox select all
			$section.find('#chk-select-all').on('change', function() {
				let checked = $(this).is(':checked');
				$section.find('.chk-row').prop('checked', checked);
			});

			// Individual 1-Click Action Remote Duty
			$section.find('.btn-action-remote').on('click', function() {
				let name = $(this).data('name');
				resolve_action([name], 'REMOTE_DUTY', page);
			});

			// Individual 1-Click Action Absent
			$section.find('.btn-action-absent').on('click', function() {
				let name = $(this).data('name');
				resolve_action([name], 'UNEXCUSED_ABSENT', page);
			});

			// Batch Remote Duty
			$section.find('#btn-batch-remote').on('click', function() {
				let selected = [];
				$section.find('.chk-row:checked').each(function() {
					selected.push($(this).data('name'));
				});

				if (selected.length === 0) {
					frappe.msgprint('Please select at least one anomaly from the checkboxes.');
					return;
				}

				resolve_action(selected, 'REMOTE_DUTY', page);
			});
		}
	});
}

function resolve_action(names, action, page) {
	frappe.call({
		method: 'hikvision_integration.axon_engine.resolve_anomaly_batch',
		args: {
			anomaly_names: names,
			action: action
		},
		freeze: true,
		freeze_message: 'Resolving anomalies...',
		callback: function(r) {
			frappe.show_alert({
				message: r.message.message || 'Anomalies resolved.',
				indicator: 'green'
			});
			load_dashboard(page);
		}
	});
}
