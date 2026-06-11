# -*- coding: utf-8 -*-
# =============================================================================
# === HUMAN ===
# The time-clock (kiosk) side of tips. When a tipped employee clocks out, the
# kiosk needs to know to show the tip keypad and where to save what they enter.
# This adds that info to the kiosk responses and a route to save the tip.
#
# === AI AGENT ===
# Subclasses core hr_attendance HrAttendance controller and OVERRIDES scan_barcode
# / manual_selection to append x_receives_tips + the last attendance id to the
# JSON response (the OWL kiosk patch in static/src reads these). save_tip is a
# NEW public jsonrpc route — auth='public' is REQUIRED (the kiosk is
# unauthenticated) and is the only legitimately public route in the module; it's
# guarded by the company token + a company-match check. Not related to the
# (login-only) timecard portal.
# =============================================================================
"""Extend the HR Attendance kiosk controller for tip / gratuity entry.

Adds ``x_receives_tips`` and the attendance record ID to every kiosk
response so the front-end can prompt for (and persist) a tip amount on
clock-out.
"""
from odoo import http
from odoo.http import request

from odoo.addons.hr_attendance.controllers.main import HrAttendance


# === HUMAN ===
# The kiosk controller with our tip additions.
# === AI AGENT ===
# Inherits the core controller so the @http.route() overrides reuse the parent
# route definitions (path/auth/type) and only change the body.
class HrAttendanceTips(HrAttendance):

    # ------------------------------------------------------------------
    # Helper – enrich a standard kiosk response with tip-tracking fields
    # === HUMAN ===
    # Tacks "does this person get tips?" and the just-created shift id onto the
    # kiosk's reply so the screen can prompt for a tip.
    # === AI AGENT ===
    # last_attendance_id is the row just opened/closed by _attendance_action_change;
    # the front-end needs its id to call save_tip after clock-out.
    # ------------------------------------------------------------------
    @staticmethod
    def _enrich_with_tip_info(response, employee):
        """Add tip-tracking fields to a kiosk employee info response."""
        if employee and response:
            response['x_receives_tips'] = employee.x_receives_tips
            if employee.last_attendance_id and 'attendance' in response:
                response['attendance']['id'] = employee.last_attendance_id.id
        return response

    # ------------------------------------------------------------------
    # Override the two kiosk routes that produce employee info responses
    # === HUMAN ===
    # The badge-scan and name+PIN clock-in/out paths — same as Odoo's, but the
    # reply is enriched with tip info.
    # === AI AGENT ===
    # Bare @http.route() reuses the parent's path/auth. We call the SAME
    # _attendance_action_change as core, then wrap _get_employee_info_response
    # with _enrich_with_tip_info. Keep the empty-dict fallthroughs (kiosk expects
    # {} on failure). The actual tip keypad/flow is in static/src OWL patches.
    # ------------------------------------------------------------------
    @http.route()
    def scan_barcode(self, token, barcode):
        company = self._get_company(token)
        if company:
            employee = request.env['hr.employee'].sudo().search(
                [('barcode', '=', barcode), ('company_id', '=', company.id)],
                limit=1,
            )
            if employee:
                employee._attendance_action_change(
                    self._get_geoip_response(
                        'kiosk',
                        device_tracking_enabled=company.attendance_device_tracking,
                    )
                )
                response = self._get_employee_info_response(employee)
                return self._enrich_with_tip_info(response, employee)
        return {}

    @http.route()
    def manual_selection(self, token, employee_id, pin_code,
                         latitude=False, longitude=False):
        company = self._get_company(token)
        if company:
            employee = request.env['hr.employee'].sudo().browse(employee_id)
            if (employee.company_id == company and
                    (not company.attendance_kiosk_use_pin or
                     employee.pin == pin_code)):
                employee.sudo()._attendance_action_change(
                    self._get_geoip_response(
                        'kiosk',
                        latitude=latitude,
                        longitude=longitude,
                        device_tracking_enabled=company.attendance_device_tracking,
                    )
                )
                response = self._get_employee_info_response(employee)
                return self._enrich_with_tip_info(response, employee)
        return {}

    # ------------------------------------------------------------------
    # New route: save the tip amount entered at the kiosk
    # === HUMAN ===
    # Stores the gratuity the employee typed on the kiosk after clocking out.
    # === AI AGENT ===
    # PUBLIC jsonrpc (kiosk is unauthenticated) — security is the company token
    # (_get_company) plus the attendance.company == token-company check. Do NOT
    # trust attendance_id without that check. Writes x_tip_amount only.
    # ------------------------------------------------------------------
    @http.route(
        '/hr_attendance/save_tip',
        type='jsonrpc',
        auth='public',
    )
    def save_tip(self, token, attendance_id, tip_amount):
        """Persist the tip amount entered at the kiosk after clock-out."""
        company = self._get_company(token)
        if not company:
            return {'status': 'error', 'message': 'Invalid token'}

        attendance = request.env['hr.attendance'].sudo().browse(attendance_id)
        if not attendance.exists():
            return {'status': 'error', 'message': 'Attendance record not found'}

        # Security: verify the attendance belongs to the same company
        if attendance.employee_id.company_id != company:
            return {'status': 'error', 'message': 'Company mismatch'}

        attendance.write({'x_tip_amount': float(tip_amount)})
        return {'status': 'ok'}
