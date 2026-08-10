frappe.pages['attendance-control-center'].on_page_load = function(wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Attendance & Payroll Control Center 🛡️',
		single_column: true
	});

	// State Variables
	page.current_page = 1;
	page.page_size = 15;
	page.filtered_anomalies = [];
	page.all_anomalies = [];

	// Add Standard Filters in Page Header
	page.company_field = page.add_field({
		fieldname: 'company',
		label: __('Company'),
		fieldtype: 'Link',
		options: 'Company',
		default: frappe.defaults.get_user_default('Company') || 'CPL Limited',
		change: function() {
			load_dashboard(page);
		}
	});

	page.from_date_field = page.add_field({
		fieldname: 'from_date',
		label: __('From Date'),
		fieldtype: 'Date',
		default: '2026-06-01',
		change: function() {
			load_dashboard(page);
		}
	});

	page.to_date_field = page.add_field({
		fieldname: 'to_date',
		label: __('To Date'),
		fieldtype: 'Date',
		default: '2026-08-31',
		change: function() {
			load_dashboard(page);
		}
	});

	page.set_secondary_action('Refresh', function() {
		load_dashboard(page);
	}, 'octicon octicon-sync');

	page.set_primary_action('Run Axon Attendance Sync ⚡', function() {
		frappe.call({
			method: 'hikvision_integration.axon_engine.process_axon_attendance',
			args: {
				from_date: page.from_date_field.get_value(),
				to_date: page.to_date_field.get_value()
			},
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
	let company = page.company_field ? page.company_field.get_value() : 'CPL Limited';
	let from_date = page.from_date_field ? page.from_date_field.get_value() : '2026-06-01';
	let to_date = page.to_date_field ? page.to_date_field.get_value() : '2026-08-31';

	let $body = $(page.body).empty();

	// 1. Fetch KPIs
	frappe.call({
		method: 'hikvision_integration.axon_engine.get_control_center_kpis',
		args: { company: company, start_date: from_date, end_date: to_date },
		callback: function(r) {
			let kpis = r.message || { today_swipes: 0, pending_anomalies: 0, approved_anomalies: 0, readiness_status: 'READY' };
			render_kpis($body, kpis);
			
			// 2. Fetch Anomalies
			frappe.call({
				method: 'hikvision_integration.axon_engine.get_anomalies_for_review',
				args: { company: company, start_date: from_date, end_date: to_date },
				callback: function(r2) {
					page.all_anomalies = r2.message || [];
					page.filtered_anomalies = page.all_anomalies.slice();
					page.current_page = 1;
					render_anomalies_section($body, page);
				}
			});
		}
	});
}

function render_kpis($container, kpis) {
	let lock_badge = kpis.pending_anomalies > 0 
		? `<span class="axon-badge axon-badge-locked">🔒 PAYROLL LOCKED (${kpis.pending_anomalies} Pending)</span>`
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

function render_anomalies_section($container, page) {
	let $section = $(`
		<div class="axon-dashboard-container">
			<div class="axon-section">
				<div class="axon-section-header" style="flex-wrap: wrap; gap: 10px;">
					<div class="axon-section-title">
						<span>🛡️ Pending Anomalies Review (<span id="txt-anomaly-count">${page.filtered_anomalies.length}</span>)</span>
					</div>
					<div style="display: flex; gap: 10px; align-items: center;">
						<input type="text" class="form-control input-sm" id="input-search-emp" placeholder="🔍 Search Employee / Dept..." style="width: 220px;">
						<button class="axon-btn axon-btn-remote" id="btn-batch-remote">
							⚡ Approve Selected as Remote Duty (8h)
						</button>
					</div>
				</div>
				<div class="axon-table-wrapper" id="anomalies-table-container"></div>
				<div id="anomalies-pagination-container" style="display: flex; justify-content: space-between; align-items: center; margin-top: 15px; padding-top: 10px; border-top: 1px solid var(--border-color, #f1f5f9);"></div>
			</div>
		</div>
	`);

	$container.append($section);

	// Bind Search Filter
	$section.find('#input-search-emp').on('keyup', function() {
		let q = $(this).val().toLowerCase();
		page.filtered_anomalies = page.all_anomalies.filter(a => {
			let name_match = (a.employee_name || '').toLowerCase().includes(q);
			let id_match = (a.employee || '').toLowerCase().includes(q);
			let dept_match = (a.department || '').toLowerCase().includes(q);
			return name_match || id_match || dept_match;
		});
		page.current_page = 1;
		render_table_page(page, $section);
	});

	// Batch Approve Selected
	$section.find('#btn-batch-remote').on('click', function() {
		let selected = [];
		$section.find('.chk-row:checked').each(function() {
			selected.push($(this).data('name'));
		});

		if (selected.length === 0) {
			frappe.msgprint('Please select at least one anomaly using the checkboxes.');
			return;
		}

		resolve_action(selected, 'REMOTE_DUTY', page);
	});

	render_table_page(page, $section);
}

function render_table_page(page, $section) {
	let list = page.filtered_anomalies;
	$section.find('#txt-anomaly-count').text(list.length);

	if (list.length === 0) {
		$section.find('#anomalies-table-container').html(`
			<div style="text-align: center; padding: 40px; color: var(--text-muted);">
				<div style="font-size: 32px; margin-bottom: 8px;">🎉</div>
				<div style="font-size: 16px; font-weight: 600;">No Pending Anomalies!</div>
				<div style="font-size: 13px; margin-top: 4px;">All check-in logs for this period are resolved and payroll is ready to run.</div>
			</div>
		`);
		$section.find('#anomalies-pagination-container').empty();
		return;
	}

	// Pagination Calculations
	let total_pages = Math.ceil(list.length / page.page_size);
	if (page.current_page > total_pages) page.current_page = total_pages;
	if (page.current_page < 1) page.current_page = 1;

	let start_idx = (page.current_page - 1) * page.page_size;
	let end_idx = Math.min(start_idx + page.page_size, list.length);
	let page_items = list.slice(start_idx, end_idx);

	let tableHtml = `
		<table class="axon-table">
			<thead>
				<tr>
					<th style="width: 30px;"><input type="checkbox" id="chk-select-all"></th>
					<th>Employee</th>
					<th>Department</th>
					<th>Date</th>
					<th>Matched Shift</th>
					<th>IN Time</th>
					<th>Reason</th>
					<th style="text-align: right;">Quick Actions</th>
				</tr>
			</thead>
			<tbody>
	`;

	page_items.forEach(a => {
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

	// Select All Checkbox
	$section.find('#chk-select-all').on('change', function() {
		$section.find('.chk-row').prop('checked', $(this).is(':checked'));
	});

	// Bind Single Actions
	$section.find('.btn-action-remote').on('click', function() {
		resolve_action([$(this).data('name')], 'REMOTE_DUTY', page);
	});

	$section.find('.btn-action-absent').on('click', function() {
		resolve_action([$(this).data('name')], 'UNEXCUSED_ABSENT', page);
	});

	// Pagination Controls HTML
	let pagHtml = `
		<div style="font-size: 13px; color: var(--text-muted);">
			Showing <strong>${start_idx + 1}–${end_idx}</strong> of <strong>${list.length}</strong> anomalies
		</div>
		<div style="display: flex; gap: 8px; align-items: center;">
			<button class="btn btn-default btn-xs" id="btn-prev-page" ${page.current_page === 1 ? 'disabled' : ''}>◄ Previous</button>
			<span style="font-size: 13px; font-weight: 600;">Page ${page.current_page} of ${total_pages}</span>
			<button class="btn btn-default btn-xs" id="btn-next-page" ${page.current_page === total_pages ? 'disabled' : ''}>Next ►</button>
		</div>
	`;

	let $pagContainer = $section.find('#anomalies-pagination-container').html(pagHtml);

	$pagContainer.find('#btn-prev-page').on('click', function() {
		if (page.current_page > 1) {
			page.current_page--;
			render_table_page(page, $section);
		}
	});

	$pagContainer.find('#btn-next-page').on('click', function() {
		if (page.current_page < total_pages) {
			page.current_page++;
			render_table_page(page, $section);
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
				message: (r.message && r.message.message) || 'Anomalies resolved.',
				indicator: 'green'
			});
			load_dashboard(page);
		}
	});
}
