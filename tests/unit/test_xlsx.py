import datetime

import openpyxl
import pytest

from rag import ingest, xlsx


def make_workbook(path, sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    wb.save(path)
    return path


def texts(path):
    return [text for text, _, _ in xlsx.chunks(path)]


def test_every_value_is_labelled_with_its_own_column_not_with_the_sheet_title(tmp_path):
    # the shape of the real report: a long title row, then the header, then data
    path = make_workbook(tmp_path / "a.xlsx", {"2026": [
        ["Information about the approved annual estimate and its execution of the whole bank"],
        ["in thousands"],
        ["No", "Expense type", "Allocated", "Actual", "Balance"],
        [1, "Khorezm building", 11159878.8999, 9635010.845, 1524868.0548999999],
        [2, "Navoi building", 40372251.34, 40372251.34, 0],
    ]})
    first, second = texts(path)
    assert "Actual: 9635010.85" in first and "Allocated: 11159878.9" in first and "Balance: 1524868.05" in first
    assert "Expense type: Khorezm building" in first
    assert "Balance: 0" in second  # zero is a value, not an empty cell
    assert "Information about the approved annual estimate" in first  # the title stays as context, once per chunk


def test_units_and_the_numbering_line_under_the_header_are_handled(tmp_path):
    path = make_workbook(tmp_path / "a.xlsx", {"S": [
        ["Title"],
        ["No", "Item", "Quantity", "Price"],
        [None, None, "(units)", "(thousand sum)"],
        [1, 2, 3, 4],
        [1, "Printer", 5, 120.5],
    ]})
    [only] = texts(path)
    assert "Price (thousand sum): 120.5" in only and "Quantity (units): 5" in only
    assert "Item: Printer" in only


def test_a_data_row_of_small_whole_numbers_is_not_mistaken_for_the_numbering_line(tmp_path):
    path = make_workbook(tmp_path / "a.xlsx", {"S": [
        ["Year", "Boys", "Girls"],
        [1, 2, 3],   # numbering line right under the header
        [2021, 30, 40],
        [2, 9, 50],  # NOT numbering: this is data (after the first data row), and not consecutive anyway
    ]})
    assert len(texts(path)) == 2
    assert any("Boys: 9" in t for t in texts(path))


def test_a_label_row_becomes_the_section_of_the_rows_below_it(tmp_path):
    path = make_workbook(tmp_path / "a.xlsx", {"S": [
        ["No", "Item", "Supplier"],
        ["Tender"],
        [1, "Firewall", "INFOTEC"],
        ["Auction"],
        [2, "Chairs", "FURNI"],
    ]})
    first, second = texts(path)
    assert "Tender" in first and "Auction" not in first
    assert "Auction" in second and "Supplier: FURNI" in second


def test_header_cells_with_line_breaks_are_collapsed_and_empty_cells_are_left_out(tmp_path):
    path = make_workbook(tmp_path / "a.xlsx", {"S": [
        ["No", "Actual\nexpenses", "Note"],
        [1, 5, None],
    ]})
    [only] = texts(path)
    assert "Actual expenses: 5" in only and "Note" not in only


def test_dates_are_written_as_dates_and_sheets_are_numbered(tmp_path):
    path = make_workbook(tmp_path / "a.xlsx", {
        "First": [["Contract", "Date", "Sum"], ["A-1", datetime.datetime(2026, 7, 10), 100]],
        "Second": [["Contract", "Date", "Sum"], ["B-2", datetime.datetime(2026, 8, 21, 14, 30), 200]],
    })
    items = xlsx.chunks(path)
    assert "Date: 2026-07-10" in items[0][0] and items[0][1:] == ([1], ["First"])
    assert "Date: 2026-08-21 14:30:00" in items[1][0] and items[1][1:] == ([2], ["Second"])


def test_a_sheet_without_a_header_row_is_skipped_and_an_empty_workbook_is_unreadable(tmp_path):
    path = make_workbook(tmp_path / "a.xlsx", {"Notes": [["just one cell"], ["another"]]})
    assert texts(path) == []
    with pytest.raises(ingest.UnreadableDocument):
        ingest.xlsx_points(path, "src", "hash")


def test_a_two_column_sheet_is_read_with_its_headers(tmp_path):
    # the commonest spreadsheet of all: a name and a value (this used to produce no chunks and fail the document)
    rows = [["Loyiha", "Byudjet"], ["Orion", 7350000], ["Vega", 1200000]]
    path = make_workbook(tmp_path / "a.xlsx", {"Byudjet": rows})
    first, second = texts(path)
    assert "Loyiha: Orion" in first and "Byudjet: 7350000" in first and "Loyiha: Vega" in second


def test_a_wide_header_still_wins_over_an_earlier_two_cell_line(tmp_path):
    path = make_workbook(tmp_path / "a.xlsx", {"S": [
        ["Hisobot", "2026-yil"], ["Nomi", "Reja", "Fakt"], ["Bino", 10, 9]]})
    (only,) = texts(path)
    assert "Reja: 10" in only and "Fakt: 9" in only and "Nomi: Bino" in only
