"""Excel files: one chunk per data row, every value labelled with its real column header.

Docling takes the first non-empty row of a sheet for the header. In reports that row is a long merged title, so every
value ends up labelled with the title and the actual column names ("Approved estimate", "Actual expenses", ...) are
lost: the model then cannot tell which number belongs to which column.
"""
import datetime
import re

from openpyxl import load_workbook

TITLE_LIMIT = 160  # characters of the sheet title repeated in every chunk (it is context, not content)


def _text(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def _format(value):
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{round(value, 2):.2f}".rstrip("0").rstrip(".")  # 268799.69999999995 -> 268799.7
    if isinstance(value, datetime.datetime):
        return value.date().isoformat() if value.time() == datetime.time() else value.isoformat(sep=" ")
    return _text(value)


def _filled(row):
    return {i: v for i, v in enumerate(row) if v is not None and _text(v) != ""}


def _is_header(row, at_least=3):
    cells = _filled(row)
    return len(cells) >= at_least and all(isinstance(v, str) for v in cells.values())


def _is_numbering(row):
    """The 1 | 2 | 3 ... line many official forms print under the header: consecutive whole numbers."""
    values = [_text(v) for v in _filled(row).values()]
    if len(values) < 3 or not all(v.isdigit() for v in values):
        return False
    numbers = [int(v) for v in values]
    return numbers[0] in (0, 1) and numbers == list(range(numbers[0], numbers[0] + len(numbers)))


def _is_annotation(row):
    """A line of notes in parentheses under the header, such as units: (thousand sum)."""
    cells = _filled(row)
    return bool(cells) and all(_text(v).startswith("(") for v in cells.values())


def _sheet_chunks(name, rows):
    """Yield the text of each data row of one sheet. `rows` are the cell values of the rows, top to bottom."""
    # A wide table's header has 3+ text cells; a narrow one (name | value) has 2. A sheet with neither is not a table.
    header_at = next((i for i, row in enumerate(rows) if _is_header(row)), None)
    if header_at is None:
        header_at = next((i for i, row in enumerate(rows) if _is_header(row, at_least=2)), None)
    if header_at is None:
        return
    title = " ".join(_text(v) for row in rows[:header_at] for v in _filled(row).values())[:TITLE_LIMIT]
    headers = {i: _text(v) for i, v in _filled(rows[header_at]).items()}
    section = ""
    seen_data = False
    for row in rows[header_at + 1:]:
        cells = _filled(row)
        if not cells or (not seen_data and _is_numbering(row)):  # numbering only ever sits right under the header
            continue
        if _is_annotation(row):  # units and notes belong to the column header
            for i, value in cells.items():
                headers[i] = f"{headers.get(i, '')} {_text(value)}".strip()
            continue
        if len(cells) == 1:  # a label that groups the rows below it
            section = _text(next(iter(cells.values())))
            continue
        seen_data = True
        fields = "; ".join(f"{headers.get(i) or f'column {i + 1}'}: {_format(v)}" for i, v in cells.items())
        yield "\n".join(part for part in (f"{name}. {title}".strip(". "), section, fields) if part)


def chunks(path):
    """Return one (text, pages, headings) per data row of every sheet; `pages` holds the sheet number."""
    workbook = load_workbook(str(path), read_only=True, data_only=True)
    result = []
    try:
        for number, sheet in enumerate(workbook.worksheets, 1):
            rows = [row for row in sheet.iter_rows(values_only=True)]
            result += [(text, [number], [sheet.title]) for text in _sheet_chunks(sheet.title, rows)]
    finally:
        workbook.close()
    return result
