"""Hyphens at the end of a line, when the lines of a footnote are joined
("Years 2019-" + "2020" came out as "Years 20192020").

Only a hyphen right after a letter can divide a word ("Towa-" + "rzystwo"):
it goes with the line break, as before. After a digit it never does - numbers
are not divided - so the hyphen stays and only the line break goes:
"2019-2020", "s. 12-15", "50-letni". When the next line repeats the hyphen,
as Polish typesetting does ("1981-" + "-2007", "polsko-" + "-niemiecki"),
one hyphen is kept. After a space or any other sign it is a dash
("1939 -" + "1945") and the line end stays as it is. Pure: no model.
"""
import re

_REPEATED = re.compile(r'-\n(?=-)')
_AFTER_DIGIT = re.compile(r'(?<=\d)-\n')
_AFTER_LETTER = re.compile(r'(?<=[^\W\d_])-\n')


def join_line_break_hyphens(text):
    text = _REPEATED.sub('', text)
    return _AFTER_LETTER.sub('', _AFTER_DIGIT.sub('-', text))
