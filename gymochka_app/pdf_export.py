import os
import json
import threading

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, PageBreak)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors

from config import PDF_FONT_PATH, PDF_FONT_BOLD_PATH


_FONT_LOCK = threading.Lock()
_FONTS_REGISTERED = False


def register_fonts():
    global _FONTS_REGISTERED
    with _FONT_LOCK:
        if _FONTS_REGISTERED: return
        if not os.path.exists(PDF_FONT_PATH):
            raise FileNotFoundError(
                f"Шрифт не найден: {PDF_FONT_PATH}. "
                "Скачайте DejaVuSans.ttf и положите в папку fonts/"
            )
        pdfmetrics.registerFont(TTFont("DejaVu", PDF_FONT_PATH))
        if os.path.exists(PDF_FONT_BOLD_PATH):
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", PDF_FONT_BOLD_PATH))
        else:
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", PDF_FONT_PATH))
        _FONTS_REGISTERED = True


def generate_pdf(user: dict, output_path: str) -> str:
    register_fonts()
    plan = json.loads(user["plan_json"])
    shopping = json.loads(user.get("shopping_json") or "[]")

    doc = SimpleDocTemplate(output_path, pagesize=A4,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm)

    styles = getSampleStyleSheet()
    title = ParagraphStyle("TitleRu", parent=styles["Title"], fontName="DejaVu-Bold",
                           fontSize=20, textColor=HexColor("#2c3e50"))
    h1 = ParagraphStyle("H1Ru", parent=styles["Heading1"], fontName="DejaVu-Bold",
                        fontSize=14, textColor=HexColor("#2980b9"))
    h2 = ParagraphStyle("H2Ru", parent=styles["Heading2"], fontName="DejaVu-Bold",
                        fontSize=11, textColor=HexColor("#34495e"))
    body = ParagraphStyle("BodyRu", parent=styles["Normal"], fontName="DejaVu",
                          fontSize=10, leading=13)
    small = ParagraphStyle("SmallRu", parent=body, fontSize=9, textColor=HexColor("#7f8c8d"))

    story = []

    story.append(Paragraph("Информационный план питания", title))
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(f"Цель: {user['target_kcal']:.0f} ккал/день", h1))
    story.append(Spacer(1, 5 * mm))

    params = [
        ["Параметр", "Значение"],
        ["Целевая калорийность", f"{user['target_kcal']:.0f} ккал"],
    ]
    t = Table(params, colWidths=[70 * mm, 90 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#3498db")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONTNAME", (0, 0), (-1, 0), "DejaVu-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "DejaVu"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#bdc3c7")),
        ("BACKGROUND", (0, 1), (-1, -1), HexColor("#ecf0f1")),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)
    story.append(PageBreak())

    story.append(Paragraph("Меню", h1))
    story.append(Spacer(1, 3 * mm))
    labels = ["Завтрак", "Перекус", "Обед", "Перекус", "Ужин"]

    for i, day in enumerate(plan, 1):
        story.append(Paragraph(f"День {i} — {day['date']}", h2))
        for label, meal in zip(labels, day["meals"]):
            text = (f"<b>{label}:</b> {meal['name']} — "
                    f"{meal['kcal']} ккал | Б:{meal['p']} Ж:{meal['f']} У:{meal['c']}")
            story.append(Paragraph(text, body))

            ingredients_text = "<b>Ингредиенты:</b> "
            ingredients_text += ", ".join(
                f"{name} — {amount} {unit}" for name, amount, unit in meal["ingredients"])
            story.append(Paragraph(ingredients_text, small))

            recipe_clean = meal["recipe"].replace("\n", "<br/>")
            story.append(Paragraph(f"<i>{recipe_clean}</i>", small))
            story.append(Spacer(1, 2 * mm))
        t = day["total"]
        story.append(Paragraph(
            f"<b>Итого:</b> {t['kcal']} ккал | Б:{t['p']} Ж:{t['f']} У:{t['c']}", body))
        story.append(Spacer(1, 4 * mm))
        if i % 3 == 0:
            story.append(PageBreak())

    story.append(PageBreak())
    story.append(Paragraph("Список покупок", h1))
    story.append(Spacer(1, 3 * mm))

    if shopping and isinstance(shopping[0], dict):
        for group in shopping:
            story.append(Paragraph(
                f"Закупка на дни {group['days_start']}–{group['days_end']}", h2))
            sh_data = [["Продукт", "Количество", "Ед."]]
            sections = group.get("sections") or {}
            if sections:
                for category, items in sections.items():
                    sh_data.append([category, "", ""])
                    for name, amount, unit in items:
                        sh_data.append([name, str(amount), unit])
            else:
                for name, amount, unit in group["items"]:
                    sh_data.append([name, str(amount), unit])
            sh = Table(sh_data, colWidths=[90 * mm, 40 * mm, 30 * mm])
            sh.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), HexColor("#27ae60")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "DejaVu-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "DejaVu"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#bdc3c7")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, HexColor("#f9f9f9")]),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(sh)
            story.append(Spacer(1, 5 * mm))
    else:
        sh_data = [["Продукт", "Количество", "Ед."]]
        for name, amount, unit in shopping:
            sh_data.append([name, str(amount), unit])
        sh = Table(sh_data, colWidths=[90 * mm, 40 * mm, 30 * mm])
        sh.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HexColor("#27ae60")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("FONTNAME", (0, 0), (-1, 0), "DejaVu-Bold"),
            ("FONTNAME", (0, 1), (-1, -1), "DejaVu"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#bdc3c7")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, HexColor("#f9f9f9")]),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(sh)

    doc.build(story)
    return output_path