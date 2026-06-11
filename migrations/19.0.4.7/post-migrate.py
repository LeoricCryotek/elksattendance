# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# A one-time data fix that runs automatically when the module is upgraded to
# this version (see the folder name). The docstring below says what it fixes.
# === AI AGENT ===
# Odoo migration: def migrate(cr, version). pre-/post- by filename. Runs only
# when upgrading ACROSS this version. Idempotent-safe. Uses raw cr or a sudo env.
# ============================================================================
"""19.0.4.7 — default clock-in/out PIN to 0000 for phone-less employees.

The kiosk PIN is derived from the phone number; employees without a phone
had no clock code. Give them '0000' (only where the PIN is empty).
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Employee = env['hr.employee']
    phone_fields = [f for f in ('work_phone', 'mobile_phone', 'private_phone')
                    if f in Employee._fields]
    candidates = Employee.search(['|', ('pin', '=', False), ('pin', '=', '')])
    targets = candidates.filtered(
        lambda e: not any(e[f] for f in phone_fields))
    if targets:
        targets.write({'pin': '0000'})
        _logger.info(
            "elksattendance 19.0.4.7: set default PIN 0000 on %d phone-less "
            "employee(s).", len(targets))
