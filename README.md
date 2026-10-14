# fatturapa-einvoice-odoo

Odoo module for importing Italian electronic invoices (FatturaPA, the XML format
used for the national SDI exchange system) and validating them automatically before
an accountant turns them into a vendor bill.

In Italy every B2B invoice has to go through the Sistema di Interscambio as an XML
file in this format. The XML carries a lot of structured data that Odoo doesn't use
out of the box, so the usual workflow is: someone downloads the XML, reads through
it, and manually types the numbers into a vendor bill. This module parses the XML
directly and runs a set of checks on it so an accountant only has to look at the
invoices that actually need a decision.

## How it's split

The module is two models merged into one (`_inherit` on the same `_name`), which
reflects how the work was actually split between two people building it together.

`einvoice_import.py` handles the XML side: it decodes the uploaded file, finds the
`FatturaElettronicaHeader` and `FatturaElettronicaBody` sections (handling the
namespace quirks some real SDI files have — some put everything in a namespace
tied to the root element, others don't), and pulls out the supplier, the invoice
metadata, the line items and the IBAN. It also checks for duplicates (same invoice
number, date and supplier VAT/Codice Fiscale already imported) and tries to match
the supplier against an existing `res.partner` by VAT or Codice Fiscale, falling
back to a "supplier missing" or "needs review" state if the match is ambiguous or
the partner record is incomplete.

`einvoice_validation.py` takes a parsed record and runs it through:

- credit note / debit note detection (TD04 / TD05), which always forces manual
  review since those adjust a prior invoice rather than creating a new cost
- VAT line checks — multiple positive rates on one invoice, lines exempt under a
  FatturaPA Natura code (N1–N7), reverse charge lines (N6.x) that need to be
  self-assessed, and lines with no rate and no Natura code at all, which usually
  means the XML is incomplete rather than the line actually being exempt
- IBAN resolution, preferring the IBAN in the XML's payment section and falling
  back to the supplier's bank account in Odoo
- payment terms / due date presence
- a keyword scan over the line descriptions (canone, abbonamento, manutenzione,
  licenza, and so on) to flag invoices that look like they cover more than one
  accounting period, so the accountant can consider deferring the cost instead of
  expensing it all at once
- basic supplier completeness (VAT, Codice Fiscale, country) since Italian tax
  reporting breaks without those

Anything that fails a check gets collected into a human-readable list on
`review_reasons` and the record goes to `needs_review`; a clean invoice goes
straight to `ready_for_bill_creation`. From there an accountant can approve or
reject it, and `prepare_invoice_data()` returns the resolved fields in the shape
needed to actually create the `account.move`.

`test_parse_standalone.py` tests just the XML parsing against a sample invoice,
without needing an Odoo instance running — useful since spinning up Odoo just to
check that a date format parses is slower than it should be.

## What's not here

This is pulled out of a larger module built for a specific client, so a few things
are missing on purpose: the actual Odoo views/XML for the form and buttons, and the
`account.move` code that calls `prepare_invoice_data()` to build the bill. Anything
that identified the client or the people involved has been renamed or removed —
the model name, log messages, docstrings. The validation logic itself and the
checks it runs are unchanged.
</content>
