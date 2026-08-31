"""Test package marker.

Without this file `python -m unittest discover -s tests -t .` fails with
"Start directory is not importable", which meant the only way to run the suite
was to invoke the single test file by path — and a suite that is awkward to run
is a suite that stops being run.
"""
