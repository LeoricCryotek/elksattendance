# -*- coding: utf-8 -*-
"""Extend the HR Attendance kiosk controller for tip / gratuity entry.

Adds ``x_receives_tips`` and the attendance record ID to every kiosk
response so the front-end can prompt for (and persist) a tip amount on
clock-out.
"""
from odoo import http
from odoo.http import request

from odoo.addons.hr_attendance.controllers.main import HrAttendance


class HrAttendanceTips(HrAttendance):

    # ------------------------------------------------------------------
    # Helper – enrich a standard kiosk response with tip-tracking fields
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
