"""
Read a CSV or Excel (.xlsx) file of companies and add/update them.

Uses only the Python standard library (an .xlsx file is a zip of XML), so no
extra package is needed on the server. Old binary .xls files aren't supported:
save them as .xlsx or CSV first.
"""

import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET

from django.db import transaction

from .models import Company
from .serializers import _generate_id, parse_bool_status

MAX_ROWS = 5000

# Accepted header names for each field (compared without spaces/underscores, case-insensitive).
HEADERS = {
    "name": ("name", "company", "companyname", "organisation", "organization"),
    "code": ("code", "companycode", "id", "companyid"),
    "email": ("email", "emailaddress", "mail"),
    "phone": ("phone", "phonenumber", "telephone", "tel", "mobile"),
    "contact_person": ("contactperson", "contact", "contactname", "hrcontact"),
    "address": ("address", "location", "officeaddress"),
    "status": ("status", "active"),
}


class UploadError(Exception):
    pass


def _norm(header):
    return re.sub(r"[^a-z0-9]", "", str(header or "").lower())


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------

def _read_csv(raw):
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in (letters.group(0) if letters else "A"):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _read_xlsx(raw):
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise UploadError("This doesn't look like an Excel (.xlsx) file. Save it as .xlsx or CSV and try again.")
    names = set(zf.namelist())

    shared = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", _NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))

    # first worksheet in workbook order
    sheet_path = "xl/worksheets/sheet1.xml"
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        first = wb.find("m:sheets/m:sheet", _NS)
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rid = first.get(_REL_NS) if first is not None else None
        for rel in rels:
            if rel.get("Id") == rid:
                target = rel.get("Target").lstrip("/")
                sheet_path = target if target.startswith("xl/") else f"xl/{target}"
    except (KeyError, ET.ParseError, AttributeError):
        pass
    if sheet_path not in names:
        raise UploadError("Couldn't find a worksheet in this Excel file.")

    rows = []
    root = ET.fromstring(zf.read(sheet_path))
    for row in root.iter(f"{{{_NS['m']}}}row"):
        values = {}
        for c in row.findall("m:c", _NS):
            kind = c.get("t")
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
            else:
                v = c.find("m:v", _NS)
                text = v.text if v is not None and v.text is not None else ""
                if kind == "s" and text != "":
                    text = shared[int(text)] if int(text) < len(shared) else ""
                elif kind == "b":
                    text = "TRUE" if text == "1" else "FALSE"
                elif kind is None and re.fullmatch(r"-?\d+\.0", text):
                    text = text[:-2]          # 8012345678.0 -> 8012345678
            values[_col_index(c.get("r"))] = text
        if values:
            width = max(values) + 1
            rows.append([values.get(i, "") for i in range(width)])
        if len(rows) > MAX_ROWS + 1:
            break
    return rows


def read_rows(filename, raw):
    name = str(filename or "").lower()
    if name.endswith(".xls"):
        raise UploadError("Old Excel (.xls) files aren't supported. Save the file as .xlsx or CSV and upload it again.")
    if name.endswith(".xlsx") or raw[:2] == b"PK":
        return _read_xlsx(raw)
    if name.endswith(".csv") or name.endswith(".txt") or not name:
        return _read_csv(raw)
    raise UploadError("Upload a CSV or Excel (.xlsx) file.")


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def import_companies(rows):
    rows = [r for r in rows if any(str(c or "").strip() for c in r)]
    if not rows:
        raise UploadError("The file is empty.")
    header = [_norm(h) for h in rows[0]]
    columns = {}
    for field, aliases in HEADERS.items():
        for i, h in enumerate(header):
            if h in aliases and i not in columns.values():
                columns[field] = i
                break
    if "name" not in columns:
        if len(header) == 1 or not any(h in sum(HEADERS.values(), ()) for h in header):
            # No recognised header: treat the first column as company names.
            columns = {"name": 0}
            data_rows = rows
        else:
            raise UploadError("The file needs a 'name' column (the company name) in the first row.")
    else:
        data_rows = rows[1:]
    if len(data_rows) > MAX_ROWS:
        raise UploadError(f"Upload at most {MAX_ROWS} companies at a time.")

    created, updated, skipped, errors = 0, 0, 0, []
    seen = set()
    existing = {c.name.lower(): c for c in Company.objects.all()}

    def cell(row, field):
        i = columns.get(field)
        return " ".join(str(row[i]).split()) if i is not None and i < len(row) and row[i] is not None else None

    with transaction.atomic():
        for line, row in enumerate(data_rows, start=2 if data_rows is not rows else 1):
            name = cell(row, "name")
            if not name:
                skipped += 1
                continue
            key = name.lower()
            if key in seen:
                skipped += 1
                errors.append(f"Row {line}: '{name}' appears more than once; the first one was used.")
                continue
            seen.add(key)
            email = cell(row, "email") or ""
            if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                errors.append(f"Row {line}: '{email}' isn't a valid email; it was left blank.")
                email = ""
            fields = {
                "code": (cell(row, "code") or "")[:50],
                "email": email[:254],
                "phone": (cell(row, "phone") or "")[:50],
                "contact_person": (cell(row, "contact_person") or "")[:200],
                "address": (cell(row, "address") or "")[:300],
            }
            raw_status = cell(row, "status")
            company = existing.get(key)
            if company:
                for f, v in fields.items():
                    if v:
                        setattr(company, f, v)
                if raw_status not in (None, ""):
                    company.status = parse_bool_status(raw_status)
                company.save()
                updated += 1
            else:
                Company.objects.create(
                    company_id=_generate_id("cmp"), name=name[:200],
                    status=parse_bool_status(raw_status) if raw_status not in (None, "") else True,
                    **fields,
                )
                created += 1
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors[:50],
        "message": f"{created} compan{'y' if created == 1 else 'ies'} added, {updated} updated"
                   + (f", {skipped} row(s) skipped" if skipped else "") + ".",
    }
