import io
import logging
import requests

from django.conf import settings
from django.core.files.storage import default_storage
from docxtpl import DocxTemplate
from docx.shared import RGBColor
from urllib.parse import unquote
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

logger = logging.getLogger(__name__)


def _fix_styles(doc: Document):
    for style_name in ["Normal", "Heading 1", "Heading 2", "Heading 3"]:
        try:    
            style = doc.styles[style_name]
            pf = style.paragraph_format
            pf.line_spacing_rule = WD_LINE_SPACING.SINGLE  
            pf.line_spacing = 1.0  # Явно фиксируем одинарный интервал
            pf.space_before = Pt(0)
            pf.space_after = Pt(0)
        except KeyError:
            continue

def _set_font(run, size_pt, bold=False, italic=False, font_name="Times New Roman"):
    run.font.name = font_name
    run.font.size = Pt(size_pt)
    run.bold = bold
    run.italic = italic

def _setup_page_numbers(doc: Document):
    section = doc.sections[0]
    section.different_first_page_header_footer = True

    footer = section.footer
    footer.is_linked_to_previous = False

    for p in footer.paragraphs:
        p.clear()

    fp = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fp.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    fp.paragraph_format.line_spacing = 1.0
    fp.paragraph_format.space_before = Pt(0)
    fp.paragraph_format.space_after = Pt(0)

    run = fp.add_run()
    _set_font(run, 12)

    fldChar1 = OxmlElement("w:fldChar")
    fldChar1.set(qn("w:fldCharType"), "begin")
    run._r.append(fldChar1)

    instrText = OxmlElement("w:instrText")
    instrText.set(qn("xml:space"), "preserve")
    instrText.text = "PAGE"
    run._r.append(instrText)

    fldChar2 = OxmlElement("w:fldChar")
    fldChar2.set(qn("w:fldCharType"), "end")
    run._r.append(fldChar2)

    first_footer = section.first_page_footer
    first_footer.is_linked_to_previous = False
    if first_footer.paragraphs:
        first_footer.paragraphs[0].clear()

    # В конце — после всех настроек секции
    pgNumType = OxmlElement("w:pgNumType")
    pgNumType.set(qn("w:start"), "1")
    section._sectPr.append(pgNumType)


def _fetch_image_bytes(content, owner=None, project_id=None):
    stored_file_id = content.get("storage_file_id")
    if stored_file_id:
        from storage.models import StoredFile
        from storage.services import download_stored_file

        stored_file = StoredFile.objects.filter(
            id=stored_file_id,
            owner=owner,
            category="document_images",
            status=StoredFile.STATUS_UPLOADED,
            metadata__project_id=str(project_id),
        ).first() if project_id else None
        if stored_file is None:
            return None
        with download_stored_file(stored_file) as file_handle:
            return file_handle.read(), stored_file.normalized_filename

    path = content.get("path")
    url = content.get("url")

    if not path and url and "/media/" in url:
        try:
            path = url.split("/media/", 1)[-1]
            path = path.split("?")[0].split("#")[0]
            path = unquote(path)
        except Exception:
            path = None

    if path:
        try:
            with default_storage.open(path, "rb") as file_handle:
                return file_handle.read(), path
        except OSError:
            logger.warning("Legacy document image is unavailable.")

    return None


def _page_break_into(p):
    run = p.add_run()
    br = OxmlElement("w:br")
    br.set(qn("w:type"), "page")
    run._r.append(br)


# ─────────────────────────────────────────────
# Шаг 1: рендер титульника через docxtpl → BytesIO
# ─────────────────────────────────────────────

def _render_title_via_template(title_content: dict, project_type: str) -> io.BytesIO:
    template_candidates = [
        os.path.join(settings.BASE_DIR, 'templates', 'word', 'base_template.docx'),
        os.path.join(settings.BASE_DIR, 'projects', 'services', 'base_template.docx'),
    ]
    template_path = next(
        (path for path in template_candidates if os.path.isfile(path)),
        None,
    )
    if not template_path:
        raise FileNotFoundError(
            "DOCX template not found in templates/word or projects/services"
        )
    doc = DocxTemplate(template_path)

    labels = {
        "course": "КУРСОВАЯ РАБОТА",
        "lab": f"ОТЧЕТ ПО ЛАБОРАТОРНОЙ РАБОТЕ №{title_content.get('lab_number', '___')}",
        "essay": "РЕФЕРАТ",
        "diplom": "ДИПЛОМНАЯ РАБОТА",
    }

    context = {
        **title_content,
        "work_type_label": labels.get(project_type, "РАБОТА"),
        "project_type": project_type,
        "show_toc": project_type != "lab",
    }

    doc.render(context)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────
# Шаг 2: открываем отрендеренный docx и добавляем контент
# ─────────────────────────────────────────────

def _render_dynamic_toc(doc: Document):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    p.paragraph_format.line_spacing = 1.0
    run = p.add_run("СОДЕРЖАНИЕ")
    _set_font(run, 16, bold=True)

    normal_style = doc.styles["Normal"]
    normal_style.font.name = "Times New Roman"
    normal_style.font.size = Pt(14)

    normal_style.element.rPr.rFonts.set(qn('w:ascii'), 'Times New Roman')
    normal_style.element.rPr.rFonts.set(qn('w:hAnsi'), 'Times New Roman')
    normal_style.element.rPr.rFonts.set(qn('w:eastAsia'), 'Times New Roman')
    normal_style.element.rPr.rFonts.set(qn('w:cs'), 'Times New Roman')

    try:
        default_font = doc.styles['Default Paragraph Font']
        for i in range(1, 4):
            style_name = f"TOC {i}"

            try:
                toc_style = doc.styles[style_name]
            except KeyError:
                continue

            toc_style.font.name = "Times New Roman"
            toc_style.font.size = Pt(14)

            pf = toc_style.paragraph_format
            pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
            pf.line_spacing = 1.0
            pf.space_before = Pt(0)
            pf.space_after = Pt(0)

            toc_style.element.rPr.rFonts.set(qn('w:ascii'), 'Times New Roman')
            toc_style.element.rPr.rFonts.set(qn('w:hAnsi'), 'Times New Roman')
            toc_style.element.rPr.rFonts.set(qn('w:eastAsia'), 'Times New Roman')
            toc_style.element.rPr.rFonts.set(qn('w:cs'), 'Times New Roman')

    except Exception:
        pass

    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.first_line_indent = Cm(0)
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    paragraph.paragraph_format.line_spacing = 1.0
    run = paragraph.add_run()

    fldChar1 = OxmlElement("w:fldChar")
    fldChar1.set(qn("w:fldCharType"), "begin")
    fldChar1.set(qn("w:dirty"), "true")
    run._r.append(fldChar1)

    instrText = OxmlElement("w:instrText")
    instrText.set(qn("xml:space"), "preserve")
    instrText.text = ' TOC \\o "1-3" \\h \\z \\u '
    run._r.append(instrText)

    fldChar2 = OxmlElement("w:fldChar")
    fldChar2.set(qn("w:fldCharType"), "separate")
    run._r.append(fldChar2)

    placeholder = OxmlElement("w:r")
    placeholder_t = OxmlElement("w:t")
    placeholder_t.text = "Обновите содержание: нажмите правой кнопкой → Обновить поле"
    placeholder.append(placeholder_t)
    paragraph._p.append(placeholder)

    run2 = paragraph.add_run()
    fldChar3 = OxmlElement("w:fldChar")
    fldChar3.set(qn("w:fldCharType"), "end")
    run2._r.append(fldChar3)
    _page_break_into(paragraph)

    settings_element = doc.settings.element
    updateFields = OxmlElement("w:updateFields")
    updateFields.set(qn("w:val"), "true")
    settings_element.append(updateFields)


def _render_heading(doc: Document, content: dict):
    level = content.get("level", 1)
    text = content.get("text", "").strip()

    if level == 1:
        is_introduction = text.lower() == "введение"
        text = text.upper() if is_introduction else (text[0].upper() + text[1:]) if text else text

    p = doc.add_paragraph(style=f"Heading {level}")
    run = p.add_run(text)

    is_introduction = text.lower() == "введение"

    if is_introduction:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Cm(0)
    else:
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT

    # НАСТРОЙКА ШРИФТА
    run.font.name = "Times New Roman"
    run.font.size = Pt(14)
    run.font.bold = True
    run.font.color.rgb = RGBColor(0, 0, 0)

    # Одинарный интервал внутри самого заголовка
    p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    p.paragraph_format.line_spacing = 1.0

    # Фикс шрифтов
    run._element.rPr.rFonts.set(qn('w:eastAsia'), 'Times New Roman')
    run._element.rPr.rFonts.set(qn('w:ascii'), 'Times New Roman')
    run._element.rPr.rFonts.set(qn('w:hAnsi'), 'Times New Roman')

    # Интервалы до и после заголовка
    if level == 1:
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(14)
    else:
        p.paragraph_format.space_before = Pt(14)
        p.paragraph_format.space_after = Pt(14)


def _render_text(doc: Document, content: dict):
    raw = content.get("text", "")
    lines = raw.split("\n")

    for line in lines:
        clean_line = line.strip()
        p = doc.add_paragraph(clean_line)

        run = p.runs[0] if p.runs else p.add_run()
        _set_font(run, 14, font_name="Times New Roman")

        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        
        # Гарантированный одинарный интервал
        p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
        p.paragraph_format.line_spacing = 1.0
        
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        if clean_line:
            p.paragraph_format.first_line_indent = Cm(1.25)


def _keep_together(p):
    pPr = p._p.get_or_add_pPr()
    kwn = OxmlElement("w:keepNext")
    pPr.append(kwn)
    kl = OxmlElement("w:keepLines")
    pPr.append(kl)


def _render_image(
    doc: Document, content: dict, img_counter: list, owner=None, project_id=None
):
    url = content.get("url", "")
    caption = content.get("caption", "")
    source = content.get("source", "")
    img_counter[0] += 1
    num = img_counter[0]

    result = _fetch_image_bytes(content, owner=owner, project_id=project_id)
    if result is None:
        p = doc.add_paragraph(f"[Изображение {num} недоступно]")
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        return

    img_bytes = result[0]
    try:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
        p.paragraph_format.line_spacing = 1.0
        _keep_together(p)  
        p.add_run().add_picture(io.BytesIO(img_bytes), width=Cm(14))

        if caption or source:
            cap_p = doc.add_paragraph()
            cap_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            cap_p.paragraph_format.first_line_indent = Cm(0)
            cap_p.paragraph_format.space_before = Pt(6)
            cap_p.paragraph_format.space_after = Pt(0)
            cap_p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
            cap_p.paragraph_format.line_spacing = 1.0
            _keep_together(cap_p)  
            
            # Подпись к рисунку делается полужирной (bold=True)
            _set_font(cap_p.add_run(f"Рисунок {num} — {caption}"), 12, bold=True)

        if source:
            src_p = doc.add_paragraph()
            src_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            src_p.paragraph_format.first_line_indent = Cm(0)
            src_p.paragraph_format.space_before = Pt(2)
            src_p.paragraph_format.space_after = Pt(12)
            src_p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
            src_p.paragraph_format.line_spacing = 1.0
            
            # Примечание остается курсивным
            _set_font(src_p.add_run(f"Примечание — Источник: {source}"), 12, italic=True)
    except Exception as e:
        logger.error(f"Ошибка вставки изображения {num}: {e}")
        doc.add_paragraph(f"[Ошибка обработки изображения {num}]")


def _set_row_min_height(row, height_twips=300):
    trPr = row._tr.get_or_add_trPr()
    for old in trPr.findall(qn('w:trHeight')):
        trPr.remove(old)
    trHeight = OxmlElement('w:trHeight')
    trHeight.set(qn('w:val'), str(height_twips))
    trHeight.set(qn('w:hRule'), 'exact')
    trPr.append(trHeight)


def _set_table_borders(table):
    tbl = table._tbl
    tblPr = tbl.tblPr

    borders = OxmlElement('w:tblBorders')

    for border_name in ['top', 'left', 'bottom', 'right', 'insideH', 'insideV']:
        border = OxmlElement(f'w:{border_name}')
        border.set(qn('w:val'), 'single')
        border.set(qn('w:sz'), '4')
        border.set(qn('w:space'), '0')
        border.set(qn('w:color'), '000000')
        borders.append(border)

    tblPr.append(borders)


def _render_table(doc: Document, content: dict):
    table_title = content.get("naming", "Без названия")
    rows_count = content.get("rows", 1)
    cols_count = content.get("cols", 1)
    data = content.get("data", [])
    table_note = content.get("source", "").strip()

    # Заголовок таблицы
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    p.paragraph_format.line_spacing = 1.0
    run = p.add_run(f"Таблица — {table_title}")
    _set_font(run, 12, bold=True)

    table = doc.add_table(rows=rows_count, cols=cols_count)
    _set_table_borders(table)

    for r in range(rows_count):
        row_obj = table.rows[r]
        _set_row_min_height(row_obj)

        for c in range(cols_count):
            cell = table.cell(r, c)
            idx = r * cols_count + c
            cell.text = str(data[idx]) if idx < len(data) else ""

            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

            for para in cell.paragraphs:
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                para.paragraph_format.left_indent = Cm(0)
                para.paragraph_format.right_indent = Cm(0)

                # Одинарный интервал внутри ячеек таблицы
                para.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
                para.paragraph_format.line_spacing = 1.0

                para.paragraph_format.space_before = Pt(0)
                para.paragraph_format.space_after = Pt(0)

                for run in para.runs:
                    run.font.name = "Times New Roman"
                    run.font.size = Pt(12)
                    run.font.bold = False
                    run.font.color.rgb = RGBColor(0, 0, 0)

                    run._element.rPr.rFonts.set(qn('w:ascii'), 'Times New Roman')
                    run._element.rPr.rFonts.set(qn('w:hAnsi'), 'Times New Roman')
                    run._element.rPr.rFonts.set(qn('w:eastAsia'), 'Times New Roman')

    # Запрещаем разрыв строки
    for row in table.rows:
        trPr = row._tr.get_or_add_trPr()
        cs = OxmlElement('w:cantSplit')
        cs.set(qn('w:val'), 'true')
        trPr.append(cs)

    # Примечание
    note_text = table_note if table_note else "Примечание — Источник: Собственная разработка"
    note_p = doc.add_paragraph()
    note_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    note_p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE  
    note_p.paragraph_format.line_spacing = 1.0                         
    note_p.paragraph_format.space_after = Pt(12)
    _set_font(note_p.add_run(note_text), 10)


def _render_chart(doc: Document, content: dict):
    title = content.get("title") or "График"
    series = content.get("series", [])
    if not isinstance(series, list) or not series:
        raise ValueError("Chart block requires at least one data series")

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_font(p.add_run(title), 12, bold=True)
    if content.get("description"):
        doc.add_paragraph(str(content["description"]))

    categories = []
    for item in series:
        if not isinstance(item, dict) or not isinstance(item.get("points"), list):
            raise ValueError("Chart series has an invalid shape")
        for point in item["points"]:
            if isinstance(point, dict) and point.get("x") not in categories:
                categories.append(point.get("x"))

    table = doc.add_table(rows=1, cols=len(series) + 1)
    _set_table_borders(table)
    table.cell(0, 0).text = str(content.get("xAxis") or "Категория")
    for index, item in enumerate(series, start=1):
        table.cell(0, index).text = str(item.get("name") or f"Ряд {index}")

    for category in categories:
        cells = table.add_row().cells
        cells[0].text = str(category)
        for column, item in enumerate(series, start=1):
            point = next(
                (
                    value
                    for value in item["points"]
                    if isinstance(value, dict) and value.get("x") == category
                ),
                None,
            )
            cells[column].text = "" if point is None else str(point.get("y", ""))
    if content.get("isDemonstration"):
        note = doc.add_paragraph("Демонстрационные данные — не фактическая статистика.")
        note.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _render_diagram(doc: Document, content: dict):
    title = content.get("title") or "Диаграмма процесса"
    nodes = content.get("nodes", [])
    edges = content.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("Diagram block requires nodes and edges")
    labels = {
        node.get("id"): node.get("label", "")
        for node in nodes
        if isinstance(node, dict)
    }

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_font(p.add_run(title), 12, bold=True)
    table = doc.add_table(rows=1, cols=2)
    _set_table_borders(table)
    table.cell(0, 0).text = "Этап"
    table.cell(0, 1).text = "Переход"
    for node in nodes:
        if isinstance(node, dict):
            row = table.add_row().cells
            row[0].text = str(node.get("label", ""))
            outgoing = [
                f"{labels.get(edge.get('to'), edge.get('to'))}"
                + (f" ({edge['label']})" if edge.get("label") else "")
                for edge in edges
                if isinstance(edge, dict) and edge.get("from") == node.get("id")
            ]
            row[1].text = " → ".join(outgoing)


# ─────────────────────────────────────────────
# Точка входа
# ─────────────────────────────────────────────

def generate_docx_from_blocks(
    blocks: list, project_type: str = "course", owner=None, project_id=None
) -> io.BytesIO:
    title_block = next((b for b in blocks if b.get("type") == "title-page"), None)
    title_content = title_block.get("content", {}) if title_block else {}

    rendered_buf = _render_title_via_template(title_content, project_type)

    doc = Document(rendered_buf)
    _fix_styles(doc)
    _setup_page_numbers(doc)

    if project_type != "lab":
        _render_dynamic_toc(doc)

    content_blocks = [b for b in blocks if b.get("type") != "title-page"]
    img_counter = [0]
    last_paragraph = [None]

    for i, block in enumerate(content_blocks):
        b_type = block.get("type")
        content = block.get("content", {})

        if b_type == "heading" and content.get("level") == 1 and i > 0:
            if last_paragraph[0] is not None:
                _page_break_into(last_paragraph[0])
            else:
                p = doc.add_paragraph()
                _page_break_into(p)

        try:
            if b_type == "heading":
                _render_heading(doc, content)
                last_paragraph[0] = doc.paragraphs[-1]
            elif b_type == "text":
                _render_text(doc, content)
                last_paragraph[0] = doc.paragraphs[-1]
            elif b_type == "image":
                _render_image(
                    doc,
                    content,
                    img_counter,
                    owner=owner,
                    project_id=project_id,
                )
                last_paragraph[0] = doc.paragraphs[-1]
            elif b_type == "table":
                _render_table(doc, content)
                last_paragraph[0] = doc.paragraphs[-1]
            elif b_type == "chart":
                _render_chart(doc, content)
                last_paragraph[0] = doc.paragraphs[-1]
            elif b_type == "diagram":
                _render_diagram(doc, content)
                last_paragraph[0] = doc.paragraphs[-1]
        except Exception as e:
            logger.error("Ошибка при рендере блока %s: %s", b_type, e)

    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output