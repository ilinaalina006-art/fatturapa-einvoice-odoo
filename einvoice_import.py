# -*- coding: utf-8 -*-

import base64
import logging
import xml.etree.ElementTree as ET
from datetime import datetime

from odoo import fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


def _safe_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(str(value).replace(',', '.'))
    except (ValueError, TypeError):
        return default


def _parse_date(date_str):
    if not date_str:
        return None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%Y%m%d'):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    _logger.warning('Could not parse date string: %s', date_str)
    return None


class EInvoiceImport(models.Model):
    _name = 'einvoice.import'
    _description = 'FatturaPA Electronic Invoice Import'
    _order = 'id desc'

    xml_file = fields.Binary(string='XML File', attachment=True)
    xml_filename = fields.Char(string='XML Filename')

    status = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('parsed', 'Parsed'),
            ('duplicate', 'Duplicate'),
            ('supplier_missing', 'Supplier Missing'),
            ('ready_for_validation', 'Ready for Validation'),
            ('needs_review', 'Needs Review'),
            ('vendor_bill_created', 'Vendor Bill Created'),
        ],
        string='Status',
        default='draft',
        required=True,
        index=True,
    )

    supplier_id = fields.Many2one(
        comodel_name='res.partner',
        string='Supplier',
        domain=[('is_company', '=', True)],
        ondelete='set null',
    )
    supplier_name = fields.Char(string='Supplier Name')
    supplier_vat = fields.Char(string='Supplier VAT')
    supplier_codice_fiscale = fields.Char(string='Supplier Codice Fiscale')

    invoice_number = fields.Char(string='Invoice Number')
    invoice_date = fields.Date(string='Invoice Date')
    document_type = fields.Char(string='Document Type')
    currency = fields.Char(string='Currency')
    total_amount = fields.Float(string='Total Amount', digits=(16, 2))
    iban = fields.Char(string='IBAN')
    due_date = fields.Date(string='Due Date')
    payment_terms_raw = fields.Char(
        string='Payment Terms (XML)',
        help='CondizioniPagamento code from XML: TP01=immediate, TP02=deferred, TP03=advance',
    )

    parsed_data = fields.Json(string='Parsed Data')
    validation_notes = fields.Text(string='Validation Notes')

    vendor_bill_id = fields.Many2one(
        comodel_name='account.move',
        string='Vendor Bill',
        ondelete='set null',
        readonly=True,
    )

    # -------------------------------------------------------------------------
    # Public action
    # -------------------------------------------------------------------------

    def action_parse_xml(self):
        self.ensure_one()

        if not self.xml_file:
            raise UserError(_('Please upload an XML file before parsing.'))

        try:
            xml_bytes = base64.b64decode(self.xml_file)
        except Exception as exc:
            raise UserError(_('Failed to decode the uploaded file: %s') % str(exc)) from exc

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise UserError(_('The uploaded file is not valid XML: %s') % str(exc)) from exc

        ns = self._detect_namespaces(root)

        header = self._find_element(root, 'FatturaElettronicaHeader', ns)
        body = self._find_element(root, 'FatturaElettronicaBody', ns)

        if header is None or body is None:
            raise UserError(
                _('The XML file does not appear to be a valid FatturaPA document '
                  '(FatturaElettronicaHeader or FatturaElettronicaBody missing).')
            )

        try:
            supplier_data = self._extract_supplier(header, ns)
            invoice_meta = self._extract_invoice_meta(body, ns)
            lines = self._extract_invoice_lines(body, ns)
            iban = self._extract_iban(header, body, ns)
        except Exception as exc:
            _logger.exception(
                'Unexpected error while parsing XML for record %s (%s)',
                self.id,
                self.xml_filename,
            )
            raise UserError(
                _('An error occurred while parsing the invoice XML: %s') % str(exc)
            ) from exc

        self.write({
            'supplier_name': supplier_data.get('name'),
            'supplier_vat': supplier_data.get('vat'),
            'supplier_codice_fiscale': supplier_data.get('codice_fiscale'),
            'invoice_number': invoice_meta.get('invoice_number'),
            'invoice_date': invoice_meta.get('invoice_date'),
            'document_type': invoice_meta.get('document_type'),
            'currency': invoice_meta.get('currency'),
            'total_amount': invoice_meta.get('total_amount', 0.0),
            'iban': iban,
            'due_date': invoice_meta.get('due_date'),
            'payment_terms_raw': invoice_meta.get('payment_terms_raw'),
            'parsed_data': {
                'supplier': supplier_data,
                'invoice': invoice_meta,
                'lines': lines,
                'iban': iban,
            },
            'status': 'parsed',
            'validation_notes': False,
        })

        _logger.info(
            'Parsed FatturaPA XML for import record %s: invoice %s from supplier %s',
            self.id,
            invoice_meta.get('invoice_number'),
            supplier_data.get('name'),
        )

        if self._check_duplicate():
            return

        self._match_supplier()

        if self.status == 'parsed':
            self.status = 'ready_for_validation'

    # -------------------------------------------------------------------------
    # XML parsing helpers
    # -------------------------------------------------------------------------

    def _detect_namespaces(self, root):
        tag = root.tag
        if tag.startswith('{'):
            uri = tag[1:tag.index('}')]
            return {'p': uri}
        return {}

    def _find_element(self, parent, local_name, ns):
        if parent is None:
            return None
        if ns:
            uri = ns.get('p', '')
            found = parent.find('{%s}%s' % (uri, local_name))
            if found is not None:
                return found
        return parent.find('.//' + local_name)

    def _find_text(self, parent, local_name, ns, default=None):
        element = self._find_element(parent, local_name, ns)
        if element is not None and element.text:
            return element.text.strip()
        return default

    def _find_all_elements(self, parent, local_name, ns):
        if parent is None:
            return []
        if ns:
            uri = ns.get('p', '')
            result = parent.findall('.//{%s}%s' % (uri, local_name))
            if result:
                return result
        return parent.findall('.//' + local_name)

    def _extract_supplier(self, header, ns):
        result = {'name': None, 'vat': None, 'codice_fiscale': None}

        cedente = self._find_element(header, 'CedentePrestatore', ns)
        if cedente is None:
            _logger.warning('CedentePrestatore not found in FatturaPA header (record %s)', self.id)
            return result

        anagrafica = self._find_element(cedente, 'Anagrafica', ns)
        if anagrafica is not None:
            denominazione = self._find_text(anagrafica, 'Denominazione', ns)
            if denominazione:
                result['name'] = denominazione
            else:
                nome = self._find_text(anagrafica, 'Nome', ns, default='')
                cognome = self._find_text(anagrafica, 'Cognome', ns, default='')
                result['name'] = (nome + ' ' + cognome).strip() or None

        id_fiscale = self._find_element(cedente, 'IdFiscaleIVA', ns)
        if id_fiscale is not None:
            paese = self._find_text(id_fiscale, 'IdPaese', ns, default='IT')
            codice = self._find_text(id_fiscale, 'IdCodice', ns)
            if codice:
                result['vat'] = (paese or 'IT') + codice

        result['codice_fiscale'] = self._find_text(cedente, 'CodiceFiscale', ns)

        return result

    def _extract_invoice_meta(self, body, ns):
        result = {
            'invoice_number': None,
            'invoice_date': None,
            'document_type': None,
            'currency': 'EUR',
            'total_amount': 0.0,
            'due_date': None,
            'payment_terms_raw': None,
        }

        dati_doc = self._find_element(body, 'DatiGeneraliDocumento', ns)
        if dati_doc is None:
            _logger.warning('DatiGeneraliDocumento not found in FatturaPA body (record %s)', self.id)
            return result

        result['document_type'] = self._find_text(dati_doc, 'TipoDocumento', ns)
        result['currency'] = self._find_text(dati_doc, 'Divisa', ns, default='EUR')
        result['invoice_number'] = self._find_text(dati_doc, 'Numero', ns)

        date_str = self._find_text(dati_doc, 'Data', ns)
        if date_str:
            result['invoice_date'] = _parse_date(date_str)

        importo_totale = self._find_text(dati_doc, 'ImportoTotaleDocumento', ns)
        result['total_amount'] = _safe_float(importo_totale)

        dati_pagamento = self._find_element(body, 'DatiPagamento', ns)
        if dati_pagamento is not None:
            result['payment_terms_raw'] = self._find_text(dati_pagamento, 'CondizioniPagamento', ns)
            due_date_str = self._find_text(dati_pagamento, 'DataScadenzaPagamento', ns)
            if due_date_str:
                result['due_date'] = _parse_date(due_date_str)

        return result

    def _extract_invoice_lines(self, body, ns):
        lines = []
        for linea in self._find_all_elements(body, 'DettaglioLinee', ns):
            line_number = self._find_text(linea, 'NumeroLinea', ns)
            description = self._find_text(linea, 'Descrizione', ns, default='')
            quantity = _safe_float(self._find_text(linea, 'Quantita', ns), default=1.0)
            unit_price = _safe_float(self._find_text(linea, 'PrezzoUnitario', ns))
            line_total = _safe_float(self._find_text(linea, 'PrezzoTotale', ns))
            vat_rate = _safe_float(self._find_text(linea, 'AliquotaIVA', ns))
            vat_nature = self._find_text(linea, 'Natura', ns)

            lines.append({
                'line_number': line_number,
                'description': description,
                'quantity': quantity,
                'unit_price': unit_price,
                'line_total': line_total,
                'vat_rate': vat_rate,
                'vat_nature': vat_nature,
            })
        return lines

    def _extract_iban(self, header, body, ns):
        iban = self._find_text(body, 'IBAN', ns)
        if not iban:
            iban = self._find_text(header, 'IBAN', ns)
        return iban

    # -------------------------------------------------------------------------
    # Duplicate detection
    # -------------------------------------------------------------------------

    def _check_duplicate(self):
        self.ensure_one()

        if not self.invoice_number or not self.invoice_date:
            _logger.debug(
                'Skipping duplicate check for record %s: invoice_number or invoice_date missing.',
                self.id,
            )
            return False

        if not self.supplier_vat and not self.supplier_codice_fiscale:
            _logger.debug(
                'Skipping duplicate check for record %s: no supplier identifier available.',
                self.id,
            )
            return False

        base_domain = [
            ('id', '!=', self.id),
            ('invoice_number', '=', self.invoice_number),
            ('invoice_date', '=', self.invoice_date),
        ]

        identifier_clauses = []
        if self.supplier_vat:
            identifier_clauses.append(('supplier_vat', '=', self.supplier_vat))
        if self.supplier_codice_fiscale:
            identifier_clauses.append(
                ('supplier_codice_fiscale', '=', self.supplier_codice_fiscale)
            )

        if len(identifier_clauses) == 2:
            id_domain = ['|'] + identifier_clauses
        else:
            id_domain = identifier_clauses

        duplicate = self.search(base_domain + id_domain, limit=1)

        if duplicate:
            _logger.info(
                'Duplicate invoice detected: record %s matches existing record %s '
                '(invoice_number=%s, invoice_date=%s)',
                self.id,
                duplicate.id,
                self.invoice_number,
                self.invoice_date,
            )
            self.write({
                'status': 'duplicate',
                'validation_notes': _(
                    'Duplicate invoice: an existing import record (ID %(dup_id)s, '
                    'invoice %(dup_number)s) already has the same invoice number, '
                    'date, and supplier identifier.'
                ) % {
                    'dup_id': duplicate.id,
                    'dup_number': duplicate.invoice_number or '—',
                },
            })
            return True

        return False

    # -------------------------------------------------------------------------
    # Supplier matching
    # -------------------------------------------------------------------------

    def _match_supplier(self):
        self.ensure_one()

        Partner = self.env['res.partner']
        matched = Partner.browse()

        if self.supplier_vat:
            matched = Partner.search([('vat', '=', self.supplier_vat)])
            if not matched:
                matched = Partner.search([('vat', '=ilike', self.supplier_vat)])

        if not matched and self.supplier_codice_fiscale:
            matched = Partner.search(
                [('l10n_it_codice_fiscale', '=', self.supplier_codice_fiscale)]
            )
            if not matched:
                matched = Partner.search(
                    [('vat', '=', self.supplier_codice_fiscale)]
                )

        if len(matched) == 0:
            _logger.info(
                'No supplier found for record %s (VAT=%s, CF=%s)',
                self.id,
                self.supplier_vat,
                self.supplier_codice_fiscale,
            )
            self.write({
                'status': 'supplier_missing',
                'validation_notes': _(
                    'No vendor found in Odoo matching VAT "%(vat)s" or '
                    'Codice Fiscale "%(cf)s". '
                    'Please create the supplier or link it manually.'
                ) % {
                    'vat': self.supplier_vat or '—',
                    'cf': self.supplier_codice_fiscale or '—',
                },
            })
            return

        if len(matched) > 1:
            partner_list = ', '.join(
                '%s (ID: %d)' % (p.name, p.id) for p in matched
            )
            _logger.warning(
                'Multiple suppliers matched for record %s: %s',
                self.id,
                partner_list,
            )
            self.write({
                'status': 'needs_review',
                'validation_notes': _(
                    'Multiple vendors matched the supplier identifiers: %(partners)s. '
                    'Please select the correct supplier manually.'
                ) % {'partners': partner_list},
            })
            return

        supplier = matched[0]
        incomplete = []
        if not supplier.vat:
            incomplete.append(_('VAT number'))
        if not supplier.street:
            incomplete.append(_('street address'))
        if not supplier.country_id:
            incomplete.append(_('country'))

        if incomplete:
            _logger.info(
                'Supplier %s (id=%d) matched for record %s but record is incomplete: %s',
                supplier.name,
                supplier.id,
                self.id,
                ', '.join(incomplete),
            )
            self.write({
                'supplier_id': supplier.id,
                'status': 'needs_review',
                'validation_notes': _(
                    'Supplier matched (%(name)s, ID: %(id)d) but the vendor record '
                    'is missing required fields: %(fields)s. '
                    'Please complete the supplier profile before proceeding.'
                ) % {
                    'name': supplier.name,
                    'id': supplier.id,
                    'fields': ', '.join(incomplete),
                },
            })
            return

        _logger.info(
            'Supplier matched for record %s: %s (id=%d)',
            self.id,
            supplier.name,
            supplier.id,
        )
        self.write({'supplier_id': supplier.id})

    # -------------------------------------------------------------------------
    # Data export helper — called to build the vendor bill
    # -------------------------------------------------------------------------

    def prepare_invoice_data(self):
        self.ensure_one()

        parsed = self.parsed_data or {}
        raw_lines = parsed.get('lines', [])

        lines = [
            {
                'description': line.get('description') or '',
                'quantity': _safe_float(line.get('quantity'), default=1.0),
                'unit_price': _safe_float(line.get('unit_price')),
                'line_total': _safe_float(line.get('line_total')),
                'vat_rate': _safe_float(line.get('vat_rate')),
                'vat_nature': line.get('vat_nature'),
            }
            for line in raw_lines
        ]

        invoice_date = self.invoice_date
        invoice_date_str = invoice_date.isoformat() if invoice_date else ''

        return {
            'invoice_number': self.invoice_number or '',
            'invoice_date': invoice_date_str,
            'document_type': self.document_type or '',
            'supplier': {
                'id': self.supplier_id.id if self.supplier_id else False,
                'name': self.supplier_name or '',
                'vat': self.supplier_vat or '',
                'codice_fiscale': self.supplier_codice_fiscale or '',
            },
            'currency': self.currency or 'EUR',
            'total_amount': self.total_amount or 0.0,
            'iban': self.effective_iban or self.iban or '',
            'lines': lines,
        }

    # -------------------------------------------------------------------------
    # Display name
    # -------------------------------------------------------------------------

    def _compute_display_name(self):
        for record in self:
            record.display_name = (
                record.invoice_number
                or record.xml_filename
                or _('Import #%d') % record.id
            )
</content>
