# -*- coding: utf-8 -*-
"""
Standalone test for the FatturaPA XML parsing logic.
No Odoo installation needed — run with: python test_parse_standalone.py
"""
import xml.etree.ElementTree as ET
from datetime import datetime

# ── helpers copied verbatim from einvoice_import.py ──────────────────────────

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
    return None


# ── minimal stub that holds the parsing methods ──────────────────────────────

class _Stub:
    id = 0

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
            return result
        anagrafica = self._find_element(cedente, 'Anagrafica', ns)
        if anagrafica is not None:
            denominazione = self._find_text(anagrafica, 'Denominazione', ns)
            result['name'] = denominazione
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
            'invoice_number': None, 'invoice_date': None,
            'document_type': None, 'currency': 'EUR',
            'total_amount': 0.0, 'due_date': None, 'payment_terms_raw': None,
        }
        dati_doc = self._find_element(body, 'DatiGeneraliDocumento', ns)
        if dati_doc is None:
            return result
        result['document_type'] = self._find_text(dati_doc, 'TipoDocumento', ns)
        result['currency'] = self._find_text(dati_doc, 'Divisa', ns, default='EUR')
        result['invoice_number'] = self._find_text(dati_doc, 'Numero', ns)
        date_str = self._find_text(dati_doc, 'Data', ns)
        if date_str:
            result['invoice_date'] = _parse_date(date_str)
        result['total_amount'] = _safe_float(self._find_text(dati_doc, 'ImportoTotaleDocumento', ns))
        dati_pag = self._find_element(body, 'DatiPagamento', ns)
        if dati_pag is not None:
            result['payment_terms_raw'] = self._find_text(dati_pag, 'CondizioniPagamento', ns)
            due = self._find_text(dati_pag, 'DataScadenzaPagamento', ns)
            if due:
                result['due_date'] = _parse_date(due)
        return result

    def _extract_invoice_lines(self, body, ns):
        lines = []
        for linea in self._find_all_elements(body, 'DettaglioLinee', ns):
            lines.append({
                'line_number': self._find_text(linea, 'NumeroLinea', ns),
                'description': self._find_text(linea, 'Descrizione', ns, default=''),
                'quantity': _safe_float(self._find_text(linea, 'Quantita', ns), default=1.0),
                'unit_price': _safe_float(self._find_text(linea, 'PrezzoUnitario', ns)),
                'line_total': _safe_float(self._find_text(linea, 'PrezzoTotale', ns)),
                'vat_rate': _safe_float(self._find_text(linea, 'AliquotaIVA', ns)),
                'vat_nature': self._find_text(linea, 'Natura', ns),
            })
        return lines

    def _extract_iban(self, header, body, ns):
        iban = self._find_text(body, 'IBAN', ns)
        if not iban:
            iban = self._find_text(header, 'IBAN', ns)
        return iban


# ── sample FatturaPA XML  (minimal, namespace on root only like real SDI files) ──

SAMPLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<p:FatturaElettronica xmlns:p="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2"
    versione="FPR12">
  <FatturaElettronicaHeader>
    <CedentePrestatore>
      <DatiAnagrafici>
        <IdFiscaleIVA>
          <IdPaese>IT</IdPaese>
          <IdCodice>01234567890</IdCodice>
        </IdFiscaleIVA>
        <CodiceFiscale>RSSMRA80A01H501U</CodiceFiscale>
        <Anagrafica>
          <Denominazione>Fornitore Test SRL</Denominazione>
        </Anagrafica>
      </DatiAnagrafici>
    </CedentePrestatore>
  </FatturaElettronicaHeader>
  <FatturaElettronicaBody>
    <DatiGenerali>
      <DatiGeneraliDocumento>
        <TipoDocumento>TD01</TipoDocumento>
        <Divisa>EUR</Divisa>
        <Data>2026-05-15</Data>
        <Numero>2026/042</Numero>
        <ImportoTotaleDocumento>1220.00</ImportoTotaleDocumento>
      </DatiGeneraliDocumento>
    </DatiGenerali>
    <DatiBeniServizi>
      <DettaglioLinee>
        <NumeroLinea>1</NumeroLinea>
        <Descrizione>Canone assistenza annuale software</Descrizione>
        <Quantita>1.00</Quantita>
        <PrezzoUnitario>1000.00</PrezzoUnitario>
        <PrezzoTotale>1000.00</PrezzoTotale>
        <AliquotaIVA>22.00</AliquotaIVA>
      </DettaglioLinee>
      <DettaglioLinee>
        <NumeroLinea>2</NumeroLinea>
        <Descrizione>Materiale di consumo</Descrizione>
        <Quantita>2.00</Quantita>
        <PrezzoUnitario>100.00</PrezzoUnitario>
        <PrezzoTotale>200.00</PrezzoTotale>
        <AliquotaIVA>0.00</AliquotaIVA>
        <Natura>N6.7</Natura>
      </DettaglioLinee>
    </DatiBeniServizi>
    <DatiPagamento>
      <CondizioniPagamento>TP02</CondizioniPagamento>
      <DettaglioPagamento>
        <ModalitaPagamento>MP05</ModalitaPagamento>
        <DataScadenzaPagamento>2026-06-15</DataScadenzaPagamento>
        <ImportoPagamento>1220.00</ImportoPagamento>
        <IBAN>IT60X0542811101000000123456</IBAN>
      </DettaglioPagamento>
    </DatiPagamento>
  </FatturaElettronicaBody>
</p:FatturaElettronica>
"""


# ── run ──────────────────────────────────────────────────────────────────────

def run():
    stub = _Stub()
    root = ET.fromstring(SAMPLE_XML)
    ns = stub._detect_namespaces(root)
    print(f"Namespace detected: {ns}")

    header = stub._find_element(root, 'FatturaElettronicaHeader', ns)
    body   = stub._find_element(root, 'FatturaElettronicaBody',   ns)

    supplier = stub._extract_supplier(header, ns)
    meta     = stub._extract_invoice_meta(body, ns)
    lines    = stub._extract_invoice_lines(body, ns)
    iban     = stub._extract_iban(header, body, ns)

    # ── assertions ───────────────────────────────────────────────────────────
    errors = []

    def check(label, actual, expected):
        if actual != expected:
            errors.append(f"FAIL  {label}: expected {expected!r}, got {actual!r}")
        else:
            print(f"  OK  {label}: {actual!r}")

    print("\n── Supplier ─────────────────────────────────────────────────────")
    check("supplier name",            supplier['name'],            'Fornitore Test SRL')
    check("supplier vat",             supplier['vat'],             'IT01234567890')
    check("supplier codice_fiscale",  supplier['codice_fiscale'],  'RSSMRA80A01H501U')

    print("\n── Invoice meta ─────────────────────────────────────────────────")
    check("document_type",       meta['document_type'],       'TD01')
    check("invoice_number",      meta['invoice_number'],      '2026/042')
    check("currency",            meta['currency'],            'EUR')
    check("total_amount",        meta['total_amount'],        1220.0)
    check("payment_terms_raw",   meta['payment_terms_raw'],   'TP02')
    check("due_date",            str(meta['due_date']),       '2026-06-15')

    print("\n── Lines ────────────────────────────────────────────────────────")
    check("line count",              len(lines),                  2)
    check("line 1 line_number",      lines[0]['line_number'],     '1')
    check("line 1 vat_rate",         lines[0]['vat_rate'],        22.0)
    check("line 1 vat_nature",       lines[0]['vat_nature'],      None)    # taxable, no Natura
    check("line 2 vat_nature",       lines[1]['vat_nature'],      'N6.7')  # reverse charge
    check("line 2 vat_rate",         lines[1]['vat_rate'],        0.0)
    check("line 1 description deferral keyword",
          'canone' in lines[0]['description'].lower(), True)

    print("\n── IBAN ─────────────────────────────────────────────────────────")
    check("iban",  iban,  'IT60X0542811101000000123456')

    print()
    if errors:
        for e in errors:
            print(e)
        raise SystemExit(f"\n{len(errors)} check(s) failed.")
    else:
        print("All checks passed.")


if __name__ == '__main__':
    run()
</content>
