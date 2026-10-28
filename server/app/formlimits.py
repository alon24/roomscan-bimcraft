"""Starlette refuses a form with more than 1000 files (HTTP 400, "Too many files"). A ring scan sends a photo and two outlines per
frame, so a 337-photo scan was 1011 files and its upload failed with the phone's -1011. The limit is there to stop abuse from the
internet; this server is for your own network, so allow far more."""
from starlette.requests import Request

MAX_PARTS = 50_000
_form = Request.form


def _form_unlimited(self, *, max_files=MAX_PARTS, max_fields=MAX_PARTS, max_part_size=1024 * 1024):
    return _form(self, max_files=max_files, max_fields=max_fields, max_part_size=max_part_size)


Request.form = _form_unlimited
