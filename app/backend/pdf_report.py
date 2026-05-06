from datetime import datetime
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)


def generate_pdf(job: dict) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title",
        parent=styles["Title"],
        fontSize=22,
        textColor=colors.HexColor("#1a1a2e"),
        spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#666666"),
        spaceAfter=20,
    )
    section_style = ParagraphStyle(
        "Section",
        parent=styles["Heading2"],
        fontSize=13,
        textColor=colors.HexColor("#16213e"),
        spaceBefore=16,
        spaceAfter=8,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#333333"),
    )
    footer_style = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#999999"),
        alignment=1,
    )

    elements = []

    # Header
    elements.append(Paragraph("Report di Ricerca Mercato", title_style))
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    elements.append(Paragraph(f"Generato il {now} · Job ID: {job.get('job_id', 'N/A')}", subtitle_style))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e0e0e0")))
    elements.append(Spacer(1, 12))

    # Prompt
    elements.append(Paragraph("Ricerca", section_style))
    elements.append(Paragraph(job.get("prompt", "N/A"), body_style))
    elements.append(Spacer(1, 8))

    # Sites visited
    sites = job.get("siti_visitati", [])
    if sites:
        elements.append(Paragraph("Siti Analizzati", section_style))
        for s in sites:
            elements.append(Paragraph(f"• {s}", body_style))
        elements.append(Spacer(1, 8))

    # Data table
    raw_data = job.get("dati", [])
    if raw_data:
        elements.append(Paragraph("Dati Estratti", section_style))
        table_data = [["Campo", "Valore", "Fonte"]]
        for row in raw_data:
            campo = str(row.get("campo", ""))
            valore = str(row.get("valore", ""))[:120]
            fonte = str(row.get("fonte", ""))[:60]
            table_data.append([
                Paragraph(campo, body_style),
                Paragraph(valore, body_style),
                Paragraph(fonte, body_style),
            ])

        col_widths = [4 * cm, 9 * cm, 4 * cm]
        t = Table(table_data, colWidths=col_widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16213e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 12))

    # AI Summary
    riassunto = job.get("riassunto", "")
    if riassunto:
        elements.append(Paragraph("Analisi AI", section_style))
        elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
        elements.append(Spacer(1, 6))
        elements.append(Paragraph(riassunto, body_style))
        elements.append(Spacer(1, 20))

    # Footer
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e0e0e0")))
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(f"Generato da Scrapling SaaS · {now}", footer_style))

    doc.build(elements)
    return buf.getvalue()
