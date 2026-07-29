from io import BytesIO
import os
from xml.sax.saxutils import escape as _xml_escape

from app.helpers import render_markdown_pdf

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    Flowable, PageBreak,
)


def _esc(value):
    """Escape a string before it's interpolated into a ReportLab Paragraph, which
    parses a restricted XML/HTML-like markup language (<b>, <font>, <a href>, etc).
    Client name/email/phone reach this function straight from the public,
    unauthenticated order form — without escaping, a submitted name like
    '<font size="40" color="red">...' renders as real styling on the generated
    quote/invoice, and unbalanced markup (an unclosed '<') raises an unhandled
    parser exception that breaks PDF generation for that document entirely.
    Applied uniformly below (including to admin-authored fields) since escaping
    a string that has no special characters is a harmless no-op.
    """
    if value is None:
        return ''
    return _xml_escape(str(value))


def _money(value):
    return f'${value:,.2f}'


def build_pdf(document_title, business, client, items, total, footer_text,
              header_text='', payment_method='', payment_details=None,
              payment_terms='', terms_of_service='', signature_enabled=False,
              document_number='', subtotal=0, surcharge_percent=0,
              notes='', logo_path=None, valid_until=None, due_date=None,
              markup_percent=0, markup_amount=0, surcharge_notes=None,
              payment_terms_font_size=9, tos_font_size=8):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        rightMargin=0.65 * inch, leftMargin=0.65 * inch,
        topMargin=0.55 * inch, bottomMargin=0.55 * inch,
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        'DocTitle', parent=styles['Title'], fontSize=22, spaceAfter=4,
        textColor=colors.HexColor('#1e293b'),
    ))
    styles.add(ParagraphStyle(
        'BizName', parent=styles['Heading2'], fontSize=14,
        textColor=colors.HexColor('#2563eb'), spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        'Muted', parent=styles['BodyText'], fontSize=9,
        textColor=colors.HexColor('#64748b'), leading=12,
    ))
    styles.add(ParagraphStyle(
        'SectionHead', parent=styles['Heading3'], fontSize=11,
        textColor=colors.HexColor('#334155'), spaceBefore=8, spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        'TOS', parent=styles['BodyText'], fontSize=8,
        textColor=colors.HexColor('#475569'), leading=11,
    ))
    styles.add(ParagraphStyle(
        'SigLine', parent=styles['BodyText'], fontSize=9,
        textColor=colors.HexColor('#94a3b8'), spaceBefore=20,
    ))
    # User-configurable sizes (Settings → PDF Templates), kept as their own styles so
    # they don't affect the other 'Muted'/'TOS' usages elsewhere on the page.
    styles.add(ParagraphStyle(
        'PaymentTermsCustom', parent=styles['BodyText'], fontSize=payment_terms_font_size,
        textColor=colors.HexColor('#64748b'), leading=payment_terms_font_size * 1.35,
    ))
    styles.add(ParagraphStyle(
        'TOSCustom', parent=styles['BodyText'], fontSize=tos_font_size,
        textColor=colors.HexColor('#475569'), leading=tos_font_size * 1.35,
    ))

    story = []

    # Header row: logo + business info | document info
    logo_cell = ''
    if logo_path and os.path.isfile(logo_path):
        try:
            logo_cell = Image(logo_path, width=0.9 * inch, height=0.55 * inch, kind='proportional')
        except Exception:
            logo_cell = ''

    biz_name_para = Paragraph(f'<b>{_esc(business.name or "Business")}</b>', styles['BizName'])
    biz_lines = []
    if business.address:
        biz_lines.append(Paragraph(_esc(business.address).replace('\n', '<br/>'), styles['Muted']))
    if getattr(business, 'abn', None):
        biz_lines.append(Paragraph(f'ABN: {_esc(business.abn)}', styles['Muted']))
    contact_parts = []
    if business.contact_email:
        contact_parts.append(_esc(business.contact_email))
    if getattr(business, 'phone', None):
        contact_parts.append(_esc(business.phone))
    if business.website:
        contact_parts.append(_esc(business.website))
    if contact_parts:
        biz_lines.append(Paragraph(' &nbsp;|&nbsp; '.join(contact_parts), styles['Muted']))

    doc_info = [
        Paragraph(document_title.upper(), styles['DocTitle']),
    ]
    if document_number:
        doc_info.append(Paragraph(f'<b>{_esc(document_number)}</b>', styles['Muted']))
    from datetime import datetime
    doc_info.append(Paragraph(f'Date: {datetime.utcnow().strftime("%d %B %Y")}', styles['Muted']))
    if valid_until:
        doc_info.append(Paragraph(f'Valid until: {valid_until.strftime("%d %B %Y")}', styles['Muted']))
    if due_date:
        doc_info.append(Paragraph(f'<b>Due: {due_date.strftime("%d %B %Y")}</b>', styles['Muted']))

    # Logo + business name share a row so the name sits next to the logo, not below it.
    # Every row of this inner table has the SAME number of columns (2), with the left
    # column left blank for the address/contact rows, so the columns never misalign.
    if logo_cell:
        left_content = [[logo_cell, biz_name_para]]
        left_col_widths = [1.05 * inch, 3.15 * inch]
    else:
        left_content = [['', biz_name_para]]
        left_col_widths = [0, 4.2 * inch]
    for line in biz_lines:
        left_content.append(['', line])

    left_table = Table(left_content, colWidths=left_col_widths)
    span_style = [
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]
    if len(left_content) > 1:
        # Let the logo image span the full height of the text block beside it,
        # so it stays vertically centered rather than only sitting next to the first line.
        span_style.append(('SPAN', (0, 0), (0, len(left_content) - 1)))
    left_table.setStyle(TableStyle(span_style))

    header_table = Table(
        [[left_table, doc_info]],
        colWidths=[4.5 * inch, 2.5 * inch],
    )
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 0.15 * inch))
    story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#e2e8f0')))
    story.append(Spacer(1, 0.15 * inch))

    if header_text:
        story.append(Paragraph(_esc(header_text).replace('\n', '<br/>'), styles['Muted']))
        story.append(Spacer(1, 0.15 * inch))

    # Client block — name/email/phone originate from the public, unauthenticated
    # order form, so these three lines are the highest-priority values to escape.
    client_lines = [Paragraph('<b>Prepared For</b>', styles['SectionHead'])]
    client_lines.append(Paragraph(f'<b>{_esc(client.name)}</b>', styles['BodyText']))
    if client.email:
        client_lines.append(Paragraph(_esc(client.email), styles['Muted']))
    if client.phone:
        client_lines.append(Paragraph(_esc(client.phone), styles['Muted']))

    story.append(Table([[client_lines]], colWidths=[7 * inch]))
    story.append(Spacer(1, 0.2 * inch))

    # Line items table
    data = [['#', 'Description', 'Details', 'Amount']]
    for i, item in enumerate(items, 1):
        data.append([
            str(i),
            item['description'] or '—',
            item.get('detail', '—'),
            _money(item['line_total']),
        ])

    col_widths = [0.35 * inch, 2.55 * inch, 3.1 * inch, 0.9 * inch]
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2563eb')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('ALIGN', (3, 1), (3, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#e2e8f0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
        ('PADDING', (0, 0), (-1, -1), 6),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.2 * inch))

    # Totals
    totals_data = []
    if subtotal and (surcharge_percent or markup_amount):
        totals_data.append(['', '', 'Subtotal:', _money(subtotal)])
        if markup_amount:
            totals_data.append(['', '', f'Markup ({markup_percent:.1f}%):', _money(markup_amount)])
        if surcharge_percent:
            surcharge_amount = total - subtotal - (markup_amount or 0)
            totals_data.append(['', '', f'Surcharge ({surcharge_percent:.1f}%):', _money(surcharge_amount)])
    totals_data.append(['', '', 'Total:', _money(total)])

    totals_table = Table(totals_data, colWidths=col_widths)
    totals_table.setStyle(TableStyle([
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
        ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
        ('FONTNAME', (2, -1), (3, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TEXTCOLOR', (3, -1), (3, -1), colors.HexColor('#2563eb')),
        ('LINEABOVE', (2, -1), (3, -1), 1, colors.HexColor('#2563eb')),
        ('PADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(totals_table)

    if notes:
        story.append(Spacer(1, 0.15 * inch))
        story.append(Paragraph('<b>Notes</b>', styles['SectionHead']))
        story.append(Paragraph(render_markdown_pdf(notes, _esc), styles['Muted']))

    # Payment details
    if payment_method or payment_details:
        story.append(Spacer(1, 0.2 * inch))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e2e8f0')))
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph('<b>Payment Information</b>', styles['SectionHead']))
        if payment_method:
            story.append(Paragraph(f'Accepted methods: {_esc(payment_method)}', styles['Muted']))
        if surcharge_notes:
            for note in surcharge_notes:
                if note:
                    story.append(Paragraph(_esc(note), styles['Muted']))
        if payment_details:
            for line in payment_details:
                if line:
                    story.append(Paragraph(_esc(line), styles['Muted']))

    if payment_terms:
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph('<b>Payment Terms</b>', styles['SectionHead']))
        story.append(Paragraph(_esc(payment_terms).replace('\n', '<br/>'), styles['PaymentTermsCustom']))

    # Agreement / TOS section
    story.append(Spacer(1, 0.25 * inch))
    story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e2e8f0')))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph('<b>Agreement &amp; Terms of Service</b>', styles['SectionHead']))

    tos_text = terms_of_service or (
        'By signing below, the client agrees to the quoted pricing, payment terms, '
        'and conditions outlined in this document. Work will commence upon acceptance '
        'and/or receipt of deposit as specified.'
    )
    story.append(Paragraph(_esc(tos_text).replace('\n', '<br/>'), styles['TOSCustom']))

    if signature_enabled:
        story.append(Spacer(1, 0.3 * inch))
        sig_table = Table([
            ['_' * 40, '', '_' * 20],
            ['Client Signature', '', 'Date'],
        ], colWidths=[3 * inch, 0.5 * inch, 2 * inch])
        sig_table.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#64748b')),
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ]))
        story.append(sig_table)

    if footer_text:
        story.append(Spacer(1, 0.2 * inch))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e2e8f0')))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(_esc(footer_text).replace('\n', '<br/>'), styles['TOS']))

    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf


def build_payment_details(business):
    lines = []
    if business.pay_id:
        lines.append(f'PayID: {business.pay_id}')
    if business.bank_account_name or business.bank_bsb or business.bank_account_number:
        bank_parts = []
        if business.bank_name:
            bank_parts.append(business.bank_name)
        if business.bank_account_name:
            bank_parts.append(business.bank_account_name)
        if business.bank_bsb:
            bank_parts.append(f'BSB: {business.bank_bsb}')
        if business.bank_account_number:
            bank_parts.append(f'Acc: {business.bank_account_number}')
        lines.append('Bank Transfer: ' + ' | '.join(bank_parts))
    if business.paypal_email:
        lines.append(f'PayPal: {business.paypal_email}')
    if business.stripe_link:
        lines.append(f'Stripe: {business.stripe_link}')
    return lines


class _CheckboxLabel(Flowable):
    """Draws a bordered square checkbox (with a real drawn checkmark stroke when
    checked, not a font glyph) immediately followed by a label. Font-rendered
    checkbox characters (☑/☐) look like plain shaded blocks in some PDF viewers
    depending on what font substitution kicks in — drawing the box and check
    ourselves renders identically everywhere.
    """
    def __init__(self, label, checked, color, box_size=12, font_size=11, gap=6):
        Flowable.__init__(self)
        self.label = label
        self.checked = checked
        self.color = color
        self.box_size = box_size
        self.font_size = font_size
        self.gap = gap
        self.width = box_size + gap + (len(label) * font_size * 0.56)
        self.height = box_size + 2

    def draw(self):
        c = self.canv
        s = self.box_size
        c.setLineWidth(1.3)
        c.setStrokeColor(self.color)
        if self.checked:
            c.setFillColor(self.color)
            c.rect(0, 0, s, s, stroke=1, fill=1)
            # Checkmark drawn as two connected strokes in white, inset from the box edges
            c.setStrokeColor(colors.white)
            c.setLineWidth(1.6)
            c.line(s * 0.22, s * 0.52, s * 0.42, s * 0.25)
            c.line(s * 0.42, s * 0.25, s * 0.82, s * 0.75)
        else:
            c.setFillColor(colors.white)
            c.rect(0, 0, s, s, stroke=1, fill=1)

        c.setFillColor(self.color)
        c.setFont('Helvetica-Bold', self.font_size)
        c.drawString(s + self.gap, (s - self.font_size) / 2 + 1, self.label)


def build_packing_slip_pdf(business, job, invoice=None, logo_path=None,
                            invoice_items=None, invoice_subtotal=0, invoice_surcharge_notes=None):
    """A two-page document meant to travel with the physical print on delivery/pickup.

    Page 1 is delivery-facing: what's in the box, print/job details, a brief paid/
    unpaid status, and a line for the business owner to sign off that it passed QC.

    Page 2 is a standalone tax invoice / receipt (line items, subtotal, surcharge,
    total, and the same paid/unpaid checkboxes) — deliberately without the terms of
    service or payment-terms text that appear on the emailed invoice PDF, since this
    is a delivery companion document, not a substitute for that invoice.

    invoice is optional — a job made without ever creating an invoice (e.g. work
    quoted informally, or an internal print) still gets a usable page 1; page 2 is
    skipped entirely rather than showing invented figures.
    """
    from datetime import datetime as _dt

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        rightMargin=0.65 * inch, leftMargin=0.65 * inch,
        topMargin=0.55 * inch, bottomMargin=0.55 * inch,
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle('DocTitle', parent=styles['Title'], fontSize=20, spaceAfter=4,
                               textColor=colors.HexColor('#1e293b')))
    styles.add(ParagraphStyle('BizName', parent=styles['Heading2'], fontSize=13,
                               textColor=colors.HexColor('#2563eb'), spaceAfter=2))
    styles.add(ParagraphStyle('Muted', parent=styles['BodyText'], fontSize=9,
                               textColor=colors.HexColor('#64748b'), leading=12))
    styles.add(ParagraphStyle('SectionHead', parent=styles['Heading3'], fontSize=11,
                               textColor=colors.HexColor('#334155'), spaceBefore=10, spaceAfter=4))
    styles.add(ParagraphStyle('Body', parent=styles['BodyText'], fontSize=9.5,
                               textColor=colors.HexColor('#1e293b'), leading=13))

    story = []

    # --- Header: logo + business name/ABN, mirroring the quote/invoice header so the
    # slip is visually recognizable as coming from the same business. ---
    logo_cell = ''
    if logo_path and os.path.isfile(logo_path):
        try:
            logo_cell = Image(logo_path, width=0.85 * inch, height=0.5 * inch, kind='proportional')
        except Exception:
            logo_cell = ''

    biz_name_para = Paragraph(f'<b>{_esc(business.name or "Business")}</b>', styles['BizName'])
    biz_lines = []
    if getattr(business, 'abn', None):
        biz_lines.append(Paragraph(f'ABN: {_esc(business.abn)}', styles['Muted']))
    contact_parts = []
    if business.contact_email:
        contact_parts.append(_esc(business.contact_email))
    if getattr(business, 'phone', None):
        contact_parts.append(_esc(business.phone))
    if contact_parts:
        biz_lines.append(Paragraph(' &nbsp;|&nbsp; '.join(contact_parts), styles['Muted']))

    if logo_cell:
        left_content = [[logo_cell, biz_name_para]]
        left_col_widths = [1.0 * inch, 3.2 * inch]
    else:
        left_content = [['', biz_name_para]]
        left_col_widths = [0, 4.2 * inch]
    for line in biz_lines:
        left_content.append(['', line])
    left_table = Table(left_content, colWidths=left_col_widths)
    left_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ] + ([('SPAN', (0, 0), (0, len(left_content) - 1))] if logo_cell else [])))

    doc_info = [
        Paragraph('DELIVERY SLIP', styles['DocTitle']),
        Paragraph(f'<b>{_esc(job.client_number)}</b>', styles['Muted']),
        Paragraph(f'Date: {_dt.utcnow().strftime("%d %B %Y")}', styles['Muted']),
    ]

    header_table = Table([[left_table, doc_info]], colWidths=[4.2 * inch, 2.55 * inch])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 0.15 * inch))
    story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#2563eb')))
    story.append(Spacer(1, 0.2 * inch))

    # --- Client / job details ---
    story.append(Paragraph('PRINT DETAILS', styles['SectionHead']))
    client_name = job.client.name if job.client else 'Walk-in / Internal'
    detail_rows = [['Client:', _esc(client_name)], ['Job:', _esc(job.title)]]
    if job.client and job.client.phone:
        detail_rows.append(['Phone:', _esc(job.client.phone)])
    if job.quote:
        detail_rows.append(['Quote Ref:', _esc(job.quote.client_number)])
    order = job.order_data
    if order.get('materials'):
        materials = order['materials']
        mat_str = ', '.join(materials) if isinstance(materials, list) else str(materials)
        if order.get('other_material'):
            mat_str = f'{mat_str}, {order["other_material"]}' if mat_str else order['other_material']
        detail_rows.append(['Material:', _esc(mat_str)])
    if order.get('description'):
        detail_rows.append(['Description:', _esc(order['description'])])

    detail_table = Table(detail_rows, colWidths=[1.1 * inch, 5.65 * inch])
    detail_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 9.5),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#64748b')),
        ('TEXTCOLOR', (1, 0), (1, -1), colors.HexColor('#1e293b')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(detail_table)

    # Line items, if the job's quote has them — gives a concrete packing-list of
    # what's actually in the box, not just a job title.
    if job.quote and job.quote.items:
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph('CONTENTS', styles['SectionHead']))
        item_rows = [['Item', 'Qty']]
        for item in job.quote.items:
            qty = item.quantity if item.item_type != 'print' else 1
            item_rows.append([_esc(item.description or 'Item'), str(qty)])
        item_table = Table(item_rows, colWidths=[5.75 * inch, 1.0 * inch])
        item_table.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 9.5),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#334155')),
            ('TEXTCOLOR', (0, 1), (-1, -1), colors.HexColor('#1e293b')),
            ('LINEBELOW', (0, 0), (-1, 0), 0.5, colors.HexColor('#e2e8f0')),
            ('ALIGN', (1, 0), (1, -1), 'CENTER'),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ]))
        story.append(item_table)

    # --- Payment status — brief on this page; the full tax invoice/receipt with line
    # items lives on its own page below so this delivery-facing page stays focused on
    # what's being handed over, not the financial breakdown. ---
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph('PAYMENT STATUS', styles['SectionHead']))
    if invoice:
        is_paid = invoice.status == 'Paid'
        paid_box = _CheckboxLabel('Paid', is_paid,
                                   colors.HexColor('#16a34a') if is_paid else colors.HexColor('#cbd5e1'))
        pending_box = _CheckboxLabel('Awaiting Payment', not is_paid,
                                      colors.HexColor('#dc2626') if not is_paid else colors.HexColor('#cbd5e1'))
        status_table = Table([[paid_box, pending_box]], colWidths=[2.2 * inch, 3.2 * inch])
        status_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(status_table)
        story.append(Paragraph(
            f'See page 2 for the tax invoice / receipt ({_esc(invoice.client_number)}).',
            styles['Muted'],
        ))
    else:
        story.append(Paragraph(
            'No invoice has been generated for this job yet.', styles['Muted'],
        ))

    # --- QC sign-off ---
    story.append(Spacer(1, 0.35 * inch))
    story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e2e8f0')))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph('QUALITY CHECK', styles['SectionHead']))
    qc_table = Table([
        ['_' * 30, '', '_' * 18],
        ['QC Checked By', '', 'Date'],
    ], colWidths=[2.9 * inch, 0.5 * inch, 2 * inch])
    qc_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#64748b')),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (1, 0), (1, 0), 14),
    ]))
    story.append(qc_table)

    story.append(Spacer(1, 0.2 * inch))
    if job.notes:
        story.append(Paragraph('NOTES', styles['SectionHead']))
        story.append(Paragraph(render_markdown_pdf(job.notes, _esc), styles['Body']))

    # --- Page 2: standalone tax invoice / receipt. Only added when there's a real
    # invoice to show — no invoice means no page 2, rather than an empty or fabricated
    # receipt. Deliberately excludes ToS and payment-terms text: this is a delivery
    # companion, not a replacement for the full invoice PDF that was (or will be) emailed. ---
    if invoice:
        story.append(PageBreak())

        title_row = Table([[
            Paragraph('TAX INVOICE / RECEIPT', styles['DocTitle']),
        ]], colWidths=[6.75 * inch])
        title_row.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 0)]))
        story.append(title_row)
        story.append(Spacer(1, 0.05 * inch))

        biz_receipt_line = f'{_esc(business.name or "Business")}'
        if getattr(business, 'abn', None):
            biz_receipt_line += f' &nbsp;|&nbsp; ABN: {_esc(business.abn)}'
        story.append(Paragraph(biz_receipt_line, styles['Muted']))
        story.append(Spacer(1, 0.1 * inch))
        story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#2563eb')))
        story.append(Spacer(1, 0.15 * inch))

        meta_rows = [
            ['Invoice Number:', _esc(invoice.client_number)],
            ['Job Reference:', _esc(job.client_number)],
            ['Date:', _dt.utcnow().strftime('%d %B %Y')],
            ['Client:', _esc(job.client.name if job.client else 'Walk-in / Internal')],
        ]
        meta_table = Table(meta_rows, colWidths=[1.5 * inch, 5.25 * inch])
        meta_table.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 9.5),
            ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#64748b')),
            ('TEXTCOLOR', (1, 0), (1, -1), colors.HexColor('#1e293b')),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 0.2 * inch))

        # Line items with real amounts, if provided by the caller
        if invoice_items:
            item_rows = [['Description', 'Amount']]
            for it in invoice_items:
                desc = it.get('description', 'Item')
                detail = it.get('detail')
                label = f'{desc}<br/><font size=7 color="#94a3b8">{_esc(detail)}</font>' if detail and detail != '—' else _esc(desc)
                item_rows.append([Paragraph(label, styles['Body']), _money(it.get('line_total', 0))])
            receipt_table = Table(item_rows, colWidths=[4.75 * inch, 2.0 * inch])
            receipt_table.setStyle(TableStyle([
                ('FONTSIZE', (0, 0), (-1, 0), 9.5),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#334155')),
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('LINEBELOW', (0, 0), (-1, 0), 0.5, colors.HexColor('#e2e8f0')),
                ('TOPPADDING', (0, 0), (-1, -1), 5),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            story.append(receipt_table)
            story.append(Spacer(1, 0.1 * inch))

        # Totals block: subtotal, surcharge notes (advisory, when multiple payment
        # methods are offered), and the final total — same math as the real invoice.
        totals_rows = []
        if invoice_items and round(invoice_subtotal, 2) != round(invoice.total, 2):
            totals_rows.append(['Subtotal:', _money(invoice_subtotal)])
            if invoice.surcharge_percent:
                totals_rows.append([f'Surcharge ({invoice.surcharge_percent:.1f}%):',
                                     _money(invoice.total - invoice_subtotal)])
        totals_rows.append(['Total:', _money(invoice.total)])
        totals_table = Table(totals_rows, colWidths=[4.75 * inch, 2.0 * inch])
        totals_style = [
            ('FONTSIZE', (0, 0), (-1, -1), 9.5),
            ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#64748b')),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('FONTSIZE', (0, -1), (-1, -1), 13),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('TEXTCOLOR', (0, -1), (-1, -1), colors.HexColor('#1e293b')),
            ('TOPPADDING', (0, -1), (-1, -1), 8),
            ('LINEABOVE', (0, -1), (-1, -1), 0.75, colors.HexColor('#cbd5e1')),
        ]
        totals_table.setStyle(TableStyle(totals_style))
        story.append(totals_table)

        if invoice_surcharge_notes:
            story.append(Spacer(1, 0.08 * inch))
            for note in invoice_surcharge_notes:
                story.append(Paragraph(_esc(note), styles['Muted']))

        # Payment status — same real checkboxes as page 1, repeated here so the
        # receipt page is self-contained if separated from page 1.
        story.append(Spacer(1, 0.3 * inch))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e2e8f0')))
        story.append(Spacer(1, 0.15 * inch))
        is_paid = invoice.status == 'Paid'
        paid_box = _CheckboxLabel('Paid', is_paid,
                                   colors.HexColor('#16a34a') if is_paid else colors.HexColor('#cbd5e1'))
        pending_box = _CheckboxLabel('Awaiting Payment', not is_paid,
                                      colors.HexColor('#dc2626') if not is_paid else colors.HexColor('#cbd5e1'))
        receipt_status_table = Table([[paid_box, pending_box]], colWidths=[2.2 * inch, 3.2 * inch])
        receipt_status_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ]))
        story.append(receipt_status_table)

        if invoice.paid_at:
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(
                f'Paid on {invoice.paid_at.strftime("%d %B %Y")}', styles['Muted'],
            ))

    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf
