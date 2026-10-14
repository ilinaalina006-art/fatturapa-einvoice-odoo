import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

# FatturaPA Natura codes indicating VAT-exempt or non-taxable lines
# N1  Excluded from scope (art. 15 DPR 633/72)
# N2  Not subject to VAT (N2.1 art.7, N2.2 other)
# N3  Non-taxable (N3.1–N3.6: exports, intra-EU supplies, etc.)
# N4  Exempt
# N5  Margin regime (art. 36 DL 41/95)
# N6  Reverse charge (N6.1–N6.9: various domestic/intra-EU categories)
# N7  VAT paid by another contracting party
VAT_EXEMPT_NATURE_CODES = frozenset({
    'N1',
    'N2', 'N2.1', 'N2.2',
    'N3', 'N3.1', 'N3.2', 'N3.3', 'N3.4', 'N3.5', 'N3.6',
    'N4',
    'N5',
    'N6', 'N6.1', 'N6.2', 'N6.3', 'N6.4', 'N6.5', 'N6.6', 'N6.7', 'N6.8', 'N6.9',
    'N7',
})

# Reverse charge sub-codes: VAT is 0% but owed by the buyer, flag for review
REVERSE_CHARGE_CODES = frozenset({
    'N6', 'N6.1', 'N6.2', 'N6.3', 'N6.4', 'N6.5', 'N6.6', 'N6.7', 'N6.8', 'N6.9',
})

# To identify document type: TD04 = credit note, TD05 = debit note
CREDIT_NOTE_TYPES = frozenset({'TD04'})
DEBIT_NOTE_TYPES = frozenset({'TD05'})

# Italian keywords in line descriptions that suggest multi-period cost (deferral candidate)
_DEFERRAL_KEYWORDS = [
    'canone',           # recurring fee / subscription charge
    'abbonamento',      # subscription
    'affitto',          # rent
    'locazione',        # lease / rental
    'manutenzione',     # maintenance (annual/monthly contracts)
    'assicurazione',    # insurance
    'leasing',
    'noleggio',         # equipment rental
    'competenza',       # accrual period marker
    'mensil',           # monthly (mensile / mensili)
    'trimestral',       # quarterly
    'semestral',        # bi-annual
    'annuale',          # annual
    'annual fee',
    'licenza',          # software/IP licence
    'assistenza',       # support contract
    'rata',             # instalment / periodic payment
    'subscription',
    'maintenance contract',
    'fornitura period', # periodic supply
]

# What do FatturaPA CondizioniPagamento codes mean
_PAYMENT_CONDITION_LABELS = {
    'TP01': 'immediate payment',
    'TP02': 'deferred payment',
    'TP03': 'advance payment',
}


class EInvoiceValidation(models.Model):
    _inherit = 'einvoice.import'

    # Extends state selection with the two states this file introduces
    # 'ready_for_bill_creation' is the success output of validation
    # 'cancelled' is set when an accountant explicitly rejects the invoice
    status = fields.Selection(
        selection_add=[
            ('ready_for_bill_creation', 'Ready for Bill Creation'),
            ('cancelled', 'Cancelled'),
        ],
        ondelete={
            'ready_for_bill_creation': 'set default',
            'cancelled': 'set default',
        },
    )

    # ── Validation result flags ───────────────────────────────────────────────
    validation_passed = fields.Boolean(
        string='Validation Passed', default=False, readonly=True,
    )
    review_reasons = fields.Text(
        string='Review Reasons', readonly=True,
        help='Bullet-point list of issues found during validation.',
    )
    is_credit_note = fields.Boolean(string='Credit Note (TD04)', default=False, readonly=True)
    is_debit_note = fields.Boolean(string='Debit Note (TD05)', default=False, readonly=True)
    has_multiple_vat_rates = fields.Boolean(
        string='Multiple VAT Rates', default=False, readonly=True,
        help='True when invoice lines carry more than one positive VAT rate',
    )
    has_vat_exempt_lines = fields.Boolean(
        string='VAT-Exempt Lines', default=False, readonly=True,
        help='True when at least one line has a FatturaPA Natura code (N1–N7)',
    )
    has_lines_missing_vat = fields.Boolean(
        string='Lines Missing VAT', default=False, readonly=True,
        help='True when at least one line has neither AliquotaIVA nor Natura.',
    )
    has_reverse_charge = fields.Boolean(
        string='Reverse Charge (N6.x)', default=False, readonly=True,
        help='True when Natura N6.x (reverse charge) is present; VAT must be self-assessed by the buyer.',
    )
    requires_deferral = fields.Boolean(
        string='Deferral Likely', default=False, readonly=True,
        help='True when line descriptions match keywords suggesting a multi-period cost.',
    )
    missing_iban = fields.Boolean(
        string='IBAN Missing', default=False, readonly=True,
    )
    payment_terms_missing = fields.Boolean(
        string='Payment Terms Missing', default=False, readonly=True,
    )
    effective_iban = fields.Char(
        string='Effective IBAN', readonly=True,
        help='IBAN resolved for this invoice (from XML first, then from partner bank account)',
    )

    # Validation actions called from the UI buttons on the invoice import form

    def action_validate_invoice(self):
        for record in self:
            if record.status not in ('parsed', 'ready_for_validation', 'needs_review'):
                continue
            record._run_all_validations()
        return True

    # approve for bill creation: accountant confirms that the invoice is ready to be turned into a
    # vendor bill; this is typically called after review in the 'needs_review' state
    def action_approve_for_bill_creation(self):
        for record in self:
            if record.status == 'needs_review':
                record.write({
                    'status': 'ready_for_bill_creation',
                    'validation_passed': True,
                })
                _logger.info(
                    'einvoice.import %s manually approved for bill creation by user %s (%s)',
                    record.id, self.env.user.name, self.env.user.id,
                )
        return True

    # reject invoice: accountant explicitly rejects the invoice import, setting state to
    # 'cancelled' to prevent any further processing; typically called when a duplicate is
    # found manually or when the invoice is deemed invalid beyond repair
    def action_reject_invoice(self):
        """Accountant decision: cancel the invoice import (e.g. duplicate found manually)."""
        for record in self:
            if record.status not in ('vendor_bill_created',):
                record.write({'status': 'cancelled'})
                _logger.info(
                    'einvoice.import %s cancelled by user %s (%s)',
                    record.id, self.env.user.name, self.env.user.id,
                )
        return True

    # The main validation logic: each check appends human-readable reasons to the list if issues
    # are found. At the end, if any reasons exist, the invoice is flagged for review with the
    # collected feedback; if no issues are found, it's marked as ready for bill creation
    def _run_all_validations(self):
        self._reset_validation_fields()
        reasons = []

        reasons += self._validate_document_type()
        reasons += self._validate_vat_lines()
        reasons += self._resolve_iban()
        reasons += self._validate_payment_terms()
        reasons += self._detect_deferral()
        reasons += self._validate_supplier_completeness()
        reasons += self._validate_invoice_date()

        if reasons:
            self.write({
                'validation_passed': False,
                'review_reasons': '\n'.join(f'• {r}' for r in reasons),
                'status': 'needs_review',
            })
        else:
            self.write({
                'validation_passed': True,
                'review_reasons': False,
                'status': 'ready_for_bill_creation',
            })

    # Reset all validation flags to their default state before running checks
    def _reset_validation_fields(self):
        self.write({
            'validation_passed': False,
            'review_reasons': False,
            'is_credit_note': False,
            'is_debit_note': False,
            'has_multiple_vat_rates': False,
            'has_vat_exempt_lines': False,
            'has_lines_missing_vat': False,
            'has_reverse_charge': False,
            'requires_deferral': False,
            'missing_iban': False,
            'payment_terms_missing': False,
            'effective_iban': False,
        })

    # Credit notes (TD04) and debit notes (TD05) always go to needs_review:
    # they adjust or cancel a prior invoice and require the accountant to
    # confirm which bill they relate to before any accounting entry is created.
    def _validate_document_type(self):
        reasons = []
        doc_type = (self.document_type or '').strip().upper()

        if doc_type in CREDIT_NOTE_TYPES:
            self.is_credit_note = True
            reasons.append(
                'Document type is TD04 (credit note). '
                'Identify the original vendor bill this credit note reduces and confirm the amount before processing.'
            )
        elif doc_type in DEBIT_NOTE_TYPES:
            self.is_debit_note = True
            reasons.append(
                'Document type is TD05 (debit note). '
                'Identify the original vendor bill this debit note supplements and confirm before processing.'
            )

        return reasons

    # Validates each invoice line individually for VAT treatment:
    # - Positive rate present = normal taxable supply; collect rate for multi-rate check.
    # - Rate 0 + Natura code = VAT-exempt or non-taxable; record which nature codes appear.
    # - Natura N6.x (reverse charge) = flag: buyer must self-assess VAT.
    # - Rate 0 + no Natura, or no rate field at all = ambiguous; flag for review.
    # - Multiple distinct positive rates = flag: each line needs individual tax mapping.
    def _validate_vat_lines(self):
        reasons = []
        lines = self._get_invoice_lines()
        if not lines:
            return reasons

        positive_rates = set()
        ambiguous_line_numbers = []
        has_exempt = False
        has_reverse_charge = False

        for line in lines:
            line_number = line.get('line_number') or line.get('NumeroLinea') or '?'
            vat_rate = line.get('vat_rate')
            vat_nature = (line.get('vat_nature') or '').strip().upper()

            # Normalise: handle None, empty string, and string representations of numbers
            rate_str = str(vat_rate).strip() if vat_rate is not None else ''
            try:
                rate_value = float(rate_str) if rate_str else None
            except ValueError:
                rate_value = None

            if vat_nature in VAT_EXEMPT_NATURE_CODES:
                has_exempt = True
                if vat_nature in REVERSE_CHARGE_CODES:
                    has_reverse_charge = True
            elif rate_value is not None and rate_value > 0:
                positive_rates.add(rate_value)
            else:
                # rate == 0 with no Natura, or no rate at all
                ambiguous_line_numbers.append(str(line_number))

        updates = {}
        if has_exempt:
            updates['has_vat_exempt_lines'] = True
        if has_reverse_charge:
            updates['has_reverse_charge'] = True
            reasons.append(
                'One or more lines carry Natura N6.x (reverse charge). '
                'The buyer must self-assess VAT on these supplies. '
                'Confirm the correct domestic reverse charge account before bill creation.'
            )
        if len(positive_rates) > 1:
            updates['has_multiple_vat_rates'] = True
            rate_list = ', '.join(f'{r:.2f}%' for r in sorted(positive_rates))
            reasons.append(
                f'Invoice contains multiple VAT rates ({rate_list}). '
                'Each line must be mapped to its correct Odoo tax individually — '
                'do not apply a single tax to the whole invoice.'
            )
        if ambiguous_line_numbers:
            updates['has_lines_missing_vat'] = True
            reasons.append(
                f'Line(s) {", ".join(ambiguous_line_numbers)} have no VAT rate and no exemption/nature code. '
                'Determine whether these lines are VAT-exempt (add the applicable Natura code) '
                'or whether the XML data is incomplete.'
            )
        if updates:
            self.write(updates)

        return reasons

    # Resolves the IBAN to use for payment:
    #   1. IBAN from the XML invoice (DatiPagamento/DettaglioPagamento/IBAN).
    #   2. If absent, fall back to the first active bank account on the partner record.
    #   3. If neither is available, flag for review (payment destination is unknown).
    # The resolved IBAN is written to effective_iban for vendor bill creation.
    def _resolve_iban(self):
        reasons = []
        iban_xml = (self.iban or '').strip()
        iban_partner = ''

        if self.supplier_id:
            partner_banks = self.supplier_id.bank_ids.filtered(lambda b: b.acc_number)
            if partner_banks:
                iban_partner = partner_banks[0].acc_number.strip()

        effective = iban_xml or iban_partner
        if effective:
            self.effective_iban = effective
        else:
            self.write({'missing_iban': True, 'effective_iban': False})
            reasons.append(
                'No IBAN found in the invoice XML and no bank account is recorded '
                'on the supplier record in Odoo. '
                'Add the supplier IBAN before the payment can be executed.'
            )
        return reasons

    # Validates that a due date or payment terms can be established for the invoice:
    #  1. Due date from the XML (DataScadenzaPagamento)
    #  2. Payment terms on the matched partner record
    # If neither is available the vendor bill cannot determine when it is due
    def _validate_payment_terms(self):
        reasons = []
        has_due_date = bool(self.due_date)
        has_terms_on_partner = bool(
            self.supplier_id
            and self.supplier_id.property_supplier_payment_term_id
        )
        # payment_terms_raw carries the raw CondizioniPagamento code (TP01/TP02/TP03)
        has_raw_condition = bool(self.payment_terms_raw)

        if not has_due_date and not has_terms_on_partner and not has_raw_condition:
            self.payment_terms_missing = True
            reasons.append(
                'No due date was found in the XML and no payment terms are set on the '
                'supplier record in Odoo. '
                'Set payment terms on the supplier or enter the due date manually before approving.'
            )
        return reasons

    # Scans line descriptions for keywords that indicate a multi-period cost
    # (e.g. subscription, rent, annual maintenance). When matched, flags the invoice
    # for deferral review so the accountant can split the cost across accounting periods
    def _detect_deferral(self):
        reasons = []
        lines = self._get_invoice_lines()
        combined_text = ' '.join((line.get('description') or '').lower() for line in lines)

        matched = [kw for kw in _DEFERRAL_KEYWORDS if kw in combined_text]
        if matched:
            self.requires_deferral = True
            shown = ', '.join(f'"{m}"' for m in matched[:5])
            reasons.append(
                f'Line descriptions contain terms suggesting a multi-period service ({shown}). '
                'Review whether this cost should be deferred across accounting periods '
                'using Odoo\'s deferral functionality before the vendor bill is posted.'
            )
        return reasons

    # Verifies that the matched Odoo partner record has sufficient data for
    # correct accounting, tax reporting, and payment processing.
    # Missing VAT or Codice Fiscale will cause issues in Italian tax reports.
    # Missing country affects SEPA payment eligibility.
    def _validate_supplier_completeness(self):
        reasons = []
        if not self.supplier_id:
            # Upstream has already set state = supplier_missing
            # validation shouldn't run in that state, but guard anyway.
            return reasons

        partner = self.supplier_id
        missing = []

        if not partner.vat:
            missing.append('VAT number (Partita IVA)')
        if not partner.country_id:
            missing.append('country')
        # l10n_it adds l10n_it_codice_fiscale; check if it's absent alongside VAT
        codice_fiscale = getattr(partner, 'l10n_it_codice_fiscale', None)
        if not codice_fiscale and not partner.vat:
            missing.append('Codice Fiscale')

        if missing:
            reasons.append(
                f'Supplier record is incomplete — missing: {", ".join(missing)}. '
                'Complete the supplier profile in Odoo before this invoice is processed.'
            )
        return reasons

    # Rejects invoices dated in the future: this typically indicates a parsing error
    # in the XML date field or an incorrectly issued invoice.
    def _validate_invoice_date(self):
        reasons = []
        if self.invoice_date and self.invoice_date > fields.Date.today():
            reasons.append(
                f'Invoice date ({self.invoice_date}) is in the future. '
                'Verify that the date was extracted correctly from the XML.'
            )
        return reasons

    # Returns the parsed invoice lines as a list of dicts.
    # Expected keys per dict: description, quantity, unit_price, total,
    # vat_rate (float or None), vat_nature (str or None).
    # Returns an empty list if the field is absent or cannot be parsed.
    def _get_invoice_lines(self):
        raw = getattr(self, 'parsed_data', None)
        if not raw:
            return []
        try:
            data = raw if isinstance(raw, dict) else {}
            return list(data.get('lines', []))
        except (TypeError, AttributeError):
            _logger.warning(
                'einvoice.import id=%s: could not read lines from parsed_data', self.id
            )
            return []
</content>
