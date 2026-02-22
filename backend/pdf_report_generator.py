"""
pdf_report_generator.py — Generate professional PDF reports for Turgon

Creates formatted PDF reports for violations, compliance summaries, and audit logs.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
        PageBreak, Image, KeepTogether
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False


def generate_violation_report_pdf(
    violations: list[dict],
    explanations: list[dict] | None = None,
    output_path: Path | str = "violation_report.pdf"
) -> Path:
    """
    Generate a professional PDF report for violations.
    
    Args:
        violations: List of violation dictionaries
        explanations: Optional list of AI explanations
        output_path: Output file path
        
    Returns:
        Path to the generated PDF file
    """
    if not REPORTLAB_AVAILABLE:
        raise ImportError("reportlab is required for PDF generation. Install with: pip install reportlab")
    
    output_path = Path(output_path)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        rightMargin=0.75*inch,
        leftMargin=0.75*inch,
        topMargin=1*inch,
        bottomMargin=0.75*inch,
    )
    
    # Container for the 'Flowable' objects
    elements = []
    
    # Define styles
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=colors.HexColor('#1d4ed8'),
        spaceAfter=30,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold'
    )
    
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=16,
        textColor=colors.HexColor('#0f172a'),
        spaceAfter=12,
        spaceBefore=12,
        fontName='Helvetica-Bold'
    )
    
    subheading_style = ParagraphStyle(
        'CustomSubHeading',
        parent=styles['Heading3'],
        fontSize=12,
        textColor=colors.HexColor('#475569'),
        spaceAfter=8,
        fontName='Helvetica-Bold'
    )
    
    normal_style = styles['Normal']
    normal_style.fontSize = 10
    normal_style.leading = 14
    
    # Title
    elements.append(Paragraph("⚖️ RuleForge Violation Report", title_style))
    elements.append(Spacer(1, 0.2*inch))
    
    # Metadata
    timestamp = datetime.now().strftime("%B %d, %Y at %H:%M:%S UTC")
    elements.append(Paragraph(f"<b>Generated:</b> {timestamp}", normal_style))
    elements.append(Spacer(1, 0.3*inch))
    
    # Executive Summary
    total_violations = sum(v.get("violation_count", 0) for v in violations)
    triggered_rules = sum(1 for v in violations if v.get("violation_count", 0) > 0)
    high_severity = sum(1 for v in violations if v.get("violation_count", 0) >= 500)
    
    elements.append(Paragraph("Executive Summary", heading_style))
    
    summary_data = [
        ["Metric", "Value"],
        ["Total Rules Checked", str(len(violations))],
        ["Rules Triggered", str(triggered_rules)],
        ["Total Violations Detected", f"{total_violations:,}"],
        ["High Severity Rules (≥500)", str(high_severity)],
    ]
    
    summary_table = Table(summary_data, colWidths=[3*inch, 2*inch])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1d4ed8')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
    ]))
    
    elements.append(summary_table)
    elements.append(Spacer(1, 0.4*inch))
    
    # Violations by Severity
    elements.append(Paragraph("Violations by Severity", heading_style))
    
    # Sort violations by count (descending)
    sorted_violations = sorted(violations, key=lambda x: x.get("violation_count", 0), reverse=True)
    
    # Only show triggered rules
    triggered = [v for v in sorted_violations if v.get("violation_count", 0) > 0]
    
    if triggered:
        for v in triggered:
            rule_id = v.get("rule_id", "?")
            count = v.get("violation_count", 0)
            description = v.get("rule_description", "No description")
            status = v.get("status", "?")
            
            # Determine severity
            if count >= 500:
                severity = "HIGH"
                color = colors.HexColor('#fee2e2')
                text_color = colors.HexColor('#dc2626')
            elif count >= 50:
                severity = "MEDIUM"
                color = colors.HexColor('#fef3c7')
                text_color = colors.HexColor('#d97706')
            else:
                severity = "LOW"
                color = colors.HexColor('#dcfce7')
                text_color = colors.HexColor('#16a34a')
            
            # Rule header
            rule_header = Paragraph(
                f"<b>{rule_id}</b> — {severity} ({count:,} violations)",
                subheading_style
            )
            
            # Description
            desc_text = Paragraph(description, normal_style)
            
            # Create a mini table for this violation
            violation_data = [
                [rule_header],
                [desc_text],
                [Paragraph(f"<b>Status:</b> {status}", normal_style)],
            ]
            
            violation_table = Table(violation_data, colWidths=[6.5*inch])
            violation_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), color),
                ('LEFTPADDING', (0, 0), (-1, -1), 12),
                ('RIGHTPADDING', (0, 0), (-1, -1), 12),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            
            elements.append(violation_table)
            elements.append(Spacer(1, 0.15*inch))
    else:
        elements.append(Paragraph("✅ No violations detected. All rules are compliant.", normal_style))
        elements.append(Spacer(1, 0.2*inch))
    
    # AI Explanations (if provided)
    if explanations:
        elements.append(PageBreak())
        elements.append(Paragraph("AI-Generated Explanations", heading_style))
        elements.append(Spacer(1, 0.2*inch))
        
        for exp in explanations:
            if exp.get("risk_level") in ("HIGH", "MEDIUM", "LOW"):
                rule_id = exp.get("rule_id", "?")
                headline = exp.get("alert_headline", "")
                plain_english = exp.get("plain_english", "")
                recommended_action = exp.get("recommended_action", "")
                
                elements.append(Paragraph(f"<b>{rule_id}:</b> {headline}", subheading_style))
                elements.append(Paragraph(plain_english, normal_style))
                elements.append(Paragraph(f"<b>Recommended Action:</b> {recommended_action}", normal_style))
                elements.append(Spacer(1, 0.2*inch))
    
    # Footer
    elements.append(Spacer(1, 0.5*inch))
    footer_style = ParagraphStyle(
        'Footer',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.grey,
        alignment=TA_CENTER
    )
    elements.append(Paragraph(
        "This report was generated by RuleForge — Autonomous Policy-to-Enforcement Engine",
        footer_style
    ))
    
    # Build PDF
    doc.build(elements)
    return output_path


def generate_compliance_report_pdf(
    violations: list[dict],
    explanations: list[dict],
    hitl_decisions: dict[str, dict],
    audit_summary: dict[str, Any],
    output_path: Path | str = "compliance_report.pdf"
) -> Path:
    """
    Generate a comprehensive compliance report PDF.
    
    Args:
        violations: List of violation dictionaries
        explanations: List of AI explanations
        hitl_decisions: Human-in-the-loop decisions
        audit_summary: Audit log summary statistics
        output_path: Output file path
        
    Returns:
        Path to the generated PDF file
    """
    if not REPORTLAB_AVAILABLE:
        raise ImportError("reportlab is required for PDF generation. Install with: pip install reportlab")
    
    output_path = Path(output_path)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        rightMargin=0.75*inch,
        leftMargin=0.75*inch,
        topMargin=1*inch,
        bottomMargin=0.75*inch,
    )
    
    elements = []
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=colors.HexColor('#1d4ed8'),
        spaceAfter=30,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold'
    )
    
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=16,
        textColor=colors.HexColor('#0f172a'),
        spaceAfter=12,
        spaceBefore=12,
        fontName='Helvetica-Bold'
    )
    
    normal_style = styles['Normal']
    
    # Title
    elements.append(Paragraph("⚖️ Compliance Report", title_style))
    elements.append(Spacer(1, 0.2*inch))
    
    # Timestamp
    timestamp = datetime.now().strftime("%B %d, %Y at %H:%M:%S UTC")
    elements.append(Paragraph(f"<b>Report Date:</b> {timestamp}", normal_style))
    elements.append(Spacer(1, 0.3*inch))
    
    # Pipeline Summary
    elements.append(Paragraph("Pipeline Summary", heading_style))
    
    total_violations = sum(v.get("violation_count", 0) for v in violations)
    triggered_rules = sum(1 for v in violations if v.get("violation_count", 0) > 0)
    
    pipeline_data = [
        ["Metric", "Value"],
        ["Rules Checked", str(len(violations))],
        ["Rules Triggered", str(triggered_rules)],
        ["Total Violations", f"{total_violations:,}"],
    ]
    
    pipeline_table = Table(pipeline_data, colWidths=[3*inch, 2*inch])
    pipeline_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1d4ed8')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
    ]))
    
    elements.append(pipeline_table)
    elements.append(Spacer(1, 0.3*inch))
    
    # HITL Summary
    elements.append(Paragraph("Human-in-the-Loop Decisions", heading_style))
    
    confirmed = sum(1 for d in hitl_decisions.values() if d.get("action") == "CONFIRMED")
    dismissed = sum(1 for d in hitl_decisions.values() if d.get("action") == "DISMISSED")
    escalated = sum(1 for d in hitl_decisions.values() if d.get("action") == "ESCALATED")
    pending = len(violations) - len(hitl_decisions)
    
    hitl_data = [
        ["Decision Type", "Count"],
        ["Confirmed", str(confirmed)],
        ["Dismissed", str(dismissed)],
        ["Escalated", str(escalated)],
        ["Pending Review", str(pending)],
    ]
    
    hitl_table = Table(hitl_data, colWidths=[3*inch, 2*inch])
    hitl_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1d4ed8')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
    ]))
    
    elements.append(hitl_table)
    elements.append(Spacer(1, 0.3*inch))
    
    # Audit Summary
    if audit_summary:
        elements.append(Paragraph("Audit Summary", heading_style))
        
        audit_data = [
            ["Metric", "Value"],
            ["Total Events", str(audit_summary.get("total_events", 0))],
            ["Pipeline Runs", str(audit_summary.get("pipeline_runs", 0))],
            ["HITL Decisions", str(audit_summary.get("hitl_decisions", 0))],
        ]
        
        audit_table = Table(audit_data, colWidths=[3*inch, 2*inch])
        audit_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1d4ed8')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('GRID', (0, 0), (-1, -1), 1, colors.grey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
        ]))
        
        elements.append(audit_table)
    
    # Footer
    elements.append(Spacer(1, 0.5*inch))
    footer_style = ParagraphStyle(
        'Footer',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.grey,
        alignment=TA_CENTER
    )
    elements.append(Paragraph(
        "This compliance report was generated by RuleForge — Autonomous Policy-to-Enforcement Engine",
        footer_style
    ))
    
    # Build PDF
    doc.build(elements)
    return output_path
