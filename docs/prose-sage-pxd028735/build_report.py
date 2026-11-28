#!/usr/bin/env python3
"""Splice the generated tables into the report template.

usage: build_report.py TEMPLATE TABLES OUT
The template must contain exactly one @@TABLES@@ marker line.
"""
import sys

tmpl_path, tables_path, out_path = sys.argv[1:4]
tmpl = open(tmpl_path, encoding="utf-8").read()
tables = open(tables_path, encoding="utf-8").read().rstrip("\n")
marker = "@@TABLES@@"
if tmpl.count(marker) != 1:
    raise SystemExit("template must contain exactly one %s marker" % marker)
if marker in tables:
    raise SystemExit("generated tables must not contain the marker")
with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(tmpl.replace(marker, tables))
print("wrote %s (%d bytes)" % (out_path, len(tmpl.replace(marker, tables))))
