from io import BytesIO
import os
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
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
        story.append(Paragraph(_esc(notes).replace('\n', '<br/>'), styles['Muted']))

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
