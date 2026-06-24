# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# One-time cleanup on upgrade: close (archive) all the old pay periods that
# piled up before this feature existed, so the approver and employees only see
# current periods. Silent — sends no email.
# === AI AGENT ===
# Calls elks.timecard._elks_close_old() which closes NON-approved cards whose
# period ended before the current period. Approved history is kept. Idempotent.
# ============================================================================
"""19.0.5.0 — close pre-existing old timecards (silent archive)."""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    n = env['elks.timecard']._elks_close_old()
    _logger.info("elksattendance 19.0.5.0: closed %s old timecard(s).", n)
