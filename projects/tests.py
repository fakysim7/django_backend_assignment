import io
from unittest.mock import patch

from docx import Document
from django.test import TestCase

from .services.word_generator import generate_docx_from_blocks


class DocumentBlockExportTests(TestCase):
    def test_existing_table_chart_and_diagram_formats_export_to_docx(self):
        template = io.BytesIO()
        Document().save(template)
        template.seek(0)
        blocks = [
            {
                "type": "table",
                "content": {
                    "naming": "Legacy table",
                    "source": "Source note",
                    "rows": 2,
                    "cols": 2,
                    "data": ["Column", "Value", "Metric", "12"],
                },
            },
            {
                "type": "chart",
                "content": {
                    "title": "Chart preview",
                    "chartType": "scatter",
                    "xAxis": "X",
                    "yAxis": "Y",
                    "series": [
                        {
                            "id": "series-1",
                            "name": "Series A",
                            "points": [{"x": "1", "y": 2}],
                        }
                    ],
                },
            },
            {
                "type": "diagram",
                "content": {
                    "title": "Flow preview",
                    "diagramType": "flowchart",
                    "nodes": [
                        {"id": "start", "label": "Start"},
                        {"id": "end", "label": "End"},
                    ],
                    "edges": [{"from": "start", "to": "end", "label": "Next"}],
                },
            },
        ]

        with patch(
            "projects.services.word_generator._render_title_via_template",
            return_value=template,
        ):
            exported = generate_docx_from_blocks(blocks, project_type="lab")

        document = Document(exported)
        exported_text = "\n".join(
            [paragraph.text for paragraph in document.paragraphs]
            + [cell.text for table in document.tables for row in table.rows for cell in row.cells]
        )
        for value in (
            "Legacy table",
            "Column",
            "Metric",
            "12",
            "Chart preview",
            "Series A",
            "Flow preview",
            "Start",
            "End",
            "Next",
        ):
            self.assertIn(value, exported_text)
