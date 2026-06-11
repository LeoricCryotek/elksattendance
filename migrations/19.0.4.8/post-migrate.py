# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# A one-time data fix that runs automatically when the module is upgraded to
# this version (see the folder name). The docstring below says what it fixes.
# === AI AGENT ===
# Odoo migration: def migrate(cr, version). pre-/post- by filename. Runs only
# when upgrading ACROSS this version. Idempotent-safe. Uses raw cr or a sudo env.
# ============================================================================
"""19.0.4.8 — remove timecards for Volunteer-department employees.

Volunteer hours are charity/volunteer time, not payroll, and never need
approval. Earlier versions auto-created (empty) timecards for them; this
deletes those so they stop showing on the portal.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cards = env['elks.timecard'].search(
        [('employee_id.department_id.name', '=', 'Volunteers')])
    if cards:
        n = len(cards)
        cards.unlink()
        _logger.info(
            "elksattendance 19.0.4.8: removed %d volunteer timecard(s).", n)
