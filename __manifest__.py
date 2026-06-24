# -*- coding: utf-8 -*-
# =============================================================================
# === HUMAN ===
# The module's "label": its name/version, what other apps it needs, and the
# ordered list of data files (security first, then views/reports/data) plus the
# kiosk front-end assets.
#
# === AI AGENT ===
# Standard Odoo manifest dict. 'depends' includes portal + website (the timecard
# portal) and elksfrs/elkscharity (lodge settings + the x_charity_task_id field
# the payroll domain excludes). 'data' order matters: security loads before the
# views that reference groups. Bump 'version' on every change so migrations in
# migrations/<version>/ run on upgrade. Assets target the public attendance bundle.
# =============================================================================
{
    "name": "Elks Attendance — Manager Security & Reporting",
    "version": "19.0.5.0",
    "category": "Human Resources",
    "summary": "Team-based attendance security and payroll timecard reports.",
    "description": """
Elks Attendance Module
======================

Extends Odoo's HR Attendance with lodge-specific enhancements:

Security
--------
* **Manager: Team Attendances** group — lets managers view and edit
  attendance records for employees who report to them (directly or
  indirectly through the org tree).  Bridges the gap between the
  built-in "see nobody" and "see everybody" attendance roles.

Reporting
---------
* **Payroll Timecard Report** — semi-monthly timecard PDF per employee
  with pay-period navigation, CSV export for QuickBooks import, and
  lodge-branded header.
* **Scheduled Timecard Email** — automatically generates and emails
  the timecard PDF to a configured recipient on a weekly, semi-monthly,
  or monthly schedule using Odoo's outgoing mail server.

How the Security Works
----------------------
A stored Many2many field (``x_manager_user_ids``) on ``hr.employee``
caches the chain of all managers above the employee.  When a manager
is changed the chain is recomputed for that employee and all their
subordinates.  The attendance record rule simply checks:

    ``('employee_id.x_manager_user_ids', 'in', user.id)``

This gives each manager read/write/create/delete access to attendance
records for everyone below them in the org tree, without needing to
assign each employee individually.
""",
    "author": "Danny Santiago",
    "website": "https://dannysantiago.info",
    "license": "LGPL-3",
    "depends": [
        "hr_attendance",
        "mail",
        "portal",
        "website",
        "elksfrs",
        "elkscharity",
    ],
    "data": [
        "security/elksattendance_security.xml",
        "security/ir.model.access.csv",
        "wizard/timecard_report_wizard_views.xml",
        "wizard/employee_link_wizard_views.xml",
        "report/timecard_report.xml",
        "views/hr_employee_views.xml",
        "views/hr_attendance_views.xml",
        "views/res_config_settings_views.xml",
        "views/elks_timecard_views.xml",
        "views/portal_templates.xml",
        "views/elksattendance_menus.xml",
        "data/timecard_cron.xml",
    ],
    "assets": {
        "hr_attendance.assets_public_attendance": [
            "elksattendance/static/src/components/**/*",
            "elksattendance/static/src/public_kiosk/**/*",
        ],
    },
    "installable": True,
    "application": False,
}
