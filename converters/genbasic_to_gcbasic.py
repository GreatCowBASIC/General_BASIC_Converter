#!/usr/bin/env python3
"""
genbasic_to_gcbasic.py
=======================

Heuristic source-to-source converter for porting BASIC style code to GCBASIC.

The script is intentionally line-oriented and best-effort rather than a full
parser. It applies a large set of mechanical rewrites, then leaves anything
that cannot be converted safely as a review marker ("'>>> REVIEW:") so the
result can be compiled and checked by hand before it is flashed to hardware.

What it currently handles
-------------------------
- Device/Xtal lines -> merged #chip directives
- Symbol/Declare assignments -> #define
- Config1..ConfigN directives -> #config
- DelayMS -> Wait n ms
- %binary and $hex literals -> 0b/0x forms
- Dword/Long typing and related helper usage
- Bare Clear/Set bit operations -> assignments to 0/1
- While/Wend -> Do While/Loop
- Repeat/Until -> Do/Loop Until
- Inc/Dec -> ++/--
- Select Case comparisons -> Case range forms, with upper bounds derived from
  the variable type when the source provides enough information
- Common serial and EEPROM helpers for Basic idioms
- Declare LCD_xxx and unsupported constructs -> commented out for review
- Pascal-style (* ... *) comments -> line comments with review markers when
  necessary

Usage
-----
    python genbasic_to_gcbasic.py source.bas
    python genbasic_to_gcbasic.py source.bas -o my_output.gcb
    python genbasic_to_gcbasic.py source.bas --xtal 20

Important notes
---------------
- The converter is heuristic and may require manual follow-up.
- Review markers are emitted whenever a transformation is lossy, ambiguous, or
  not fully safe to automate.
- Always compile the output with the real GCBASIC compiler and inspect the
  REVIEW notes before flashing generated code to a chip.

Transformation strategy
----------------------
1. Normalize source lines and strip or rewrite obvious BASIC syntax.
2. Convert declarations, directives, literals, and control-flow forms to
   GCBASIC equivalents.
3. Replace unsupported or ambiguous constructs with comments or helper-based
   rewrites that are flagged for review.
4. Append helper subs when the generated code needs EEPROM or device-specific
   support that GCBASIC does not provide directly.
"""

import argparse
import platform
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------
# Regex patterns (compiled once)
# ---------------------------------------------------------------------

RE_DEVICE = re.compile(r'^\s*Device\s*=\s*(\S+)\s*$', re.IGNORECASE)
RE_XTAL = re.compile(r'^\s*Xtal\s*=\s*(\S+)\s*$', re.IGNORECASE)

RE_SYMBOL = re.compile(r'^(\s*)Symbol\s+(\w+)\s*=\s*(.+?)\s*$', re.IGNORECASE)

# "Declare NAME = VALUE" or "Declare NAME VALUE" (e.g. "Declare
# Hserial_Baud = 9600", "Declare HSERIN_PIN PORTC.5") is a compile-time
# constant assignment, same idea as Symbol - GCBASIC has no "Declare"
# form for this, so both spellings become "#define NAME VALUE". The '='
# is optional since BASIC accepts either. Doesn't match "Declare
# LCD_xxx ..." (handled separately above, checked first).
RE_DECLARE_ASSIGN = re.compile(r'^(\s*)Declare\s+(\w+)\s*=?\s*(.+?)\s*$', re.IGNORECASE)

# PIC18F/enhanced-core-style "Config1".."Config7" fuse-setting lines are
# all just GCBASIC's single #config directive - the trailing number is a
# BASIC-ism with no GCBASIC meaning, so it's dropped entirely
# ("Config5 CP_OFF" -> "#Config CP_OFF").
RE_CONFIG_LINE = re.compile(r'^(\s*)Config[1-9]\d*\b(.*)$', re.IGNORECASE)

# --- Serial commands -------------------------------------------------
# GCBASIC has no BASIC-style bracketed item-list serial statements.
# Hardware USART: HSerOut [a, b] -> HSerSend a / HSerSend b (one call per
# item); HSerIn timeout,label,[a,b] -> a = HSerReceive / b = HSerReceive,
# each followed by an "If x = 255 Then GoTo label" (255 is HSerReceive's
# "no new data" sentinel when USART_BLOCKING isn't defined - the closest
# available approximation of BASIC's per-byte timeout, flagged below
# since it isn't exact and 255 can also be a genuine received byte).
# --- Command lookup table ---------------------------------------------
# A single, easily-extended table of "old BASIC construct -> new
# GCBASIC guidance" entries, for the class of one-off statement-level
# substitutions that don't need their own bespoke logic. Two kinds:
#
#   "rewrite"     - the match is replaced outright with new GCBASIC
#                   syntax (e.g. a bit-alias Dim, or an Asm/EndAsm
#                   marker).
#   "comment_out" - the original line is kept but commented out with a
#                   "//!" marker, with a "//! use XXX" hint added on the
#                   line above it, for constructs with no safe automatic
#                   1:1 rewrite (e.g. CErase/CWrite -> HEFEraseBlock/
#                   HEFWriteBlock, which have different argument shapes).
#
# To add a new one-off replacement, add an entry here - no other code
# needs to change. Each match is also counted in lookup_usage (passed
# through convert_line) so main() can print one summary line per
# construct actually found, rather than one per occurrence.
def _rewrite_bit_alias_dim(m):
    indent, var, alias_var, alias_bit = m.groups()
    return f"{indent}Dim {var} As Bit Alias {alias_var}.{alias_bit}"


def _rewrite_asm_marker(m):
    return f"{m.group(1)}#asmraw"


COMMAND_LOOKUP = [
    {
        "name": "Bit-alias Dim (Dim x As y.n)",
        "kind": "rewrite",
        "pattern": re.compile(r'^(\s*)Dim\s+(\w+)\s+As\s+(\w+)\.(\w+)\s*$', re.IGNORECASE),
        "rewrite": _rewrite_bit_alias_dim,
    },
    {
        "name": "Asm block start",
        "kind": "rewrite",
        "pattern": re.compile(r'^(\s*)Asm\s*$', re.IGNORECASE),
        "rewrite": _rewrite_asm_marker,
    },
    {
        "name": "EndAsm block end",
        "kind": "rewrite",
        "pattern": re.compile(r'^(\s*)EndAsm\s*$', re.IGNORECASE),
        "rewrite": _rewrite_asm_marker,
    },
    {
        "name": "CErase",
        "kind": "comment_out",
        "pattern": re.compile(r'^(\s*)(CErase\b.*)$', re.IGNORECASE),
        "hint": "use HEFEraseBlock",
    },
    {
        "name": "CWrite",
        "kind": "comment_out",
        "pattern": re.compile(r'^(\s*)(CWrite\b.*)$', re.IGNORECASE),
        "hint": "use HEFWriteBlock",
    },
]


def apply_command_lookup(code, comment, newline, lookup_usage):
    """Check `code` against every COMMAND_LOOKUP entry in order. Returns
    the replacement text (including newline) on the first match, or None
    if nothing matched. Increments lookup_usage[entry_name] on a hit."""
    for entry in COMMAND_LOOKUP:
        m = entry["pattern"].match(code)
        if not m:
            continue
        lookup_usage[entry["name"]] = lookup_usage.get(entry["name"], 0) + 1
        if entry["kind"] == "rewrite":
            new_code = entry["rewrite"](m)
            return f"{new_code}{('  ' + comment) if comment else ''}{newline}"
        else:  # comment_out
            indent, rest = m.group(1), m.group(2)
            hint_line = f"{indent}//! {entry['hint']}{newline}"
            commented_line = f"{indent}//! {rest}{('  ' + comment) if comment else ''}{newline}"
            return hint_line + commented_line
    return None


RE_HSEROUT = re.compile(r'^(\s*)HSerOut\s*\[\s*(.*?)\s*\]\s*$', re.IGNORECASE)
RE_HSERIN = re.compile(
    r'^(\s*)HSerIn\s+([^\s,]+)\s*,\s*([^\s,\[\]]+)\s*,\s*\[\s*(.*?)\s*\]\s*$',
    re.IGNORECASE,
)

# Software serial: SerOut pin,baudmode,[items] -> Ser1Send item (one call
# per item); SerIn pin,baudmode,[items] -> item = Ser1Receive. GCBASIC's
# Ser1Send/Ser1Receive channel needs SER1_BAUD/SER1_TXPORT/SER1_TXPIN (and
# SER1_RXPORT/SER1_RXPIN for receiving) #defined once - see
# find_serial_config() - resolved from the pin symbol and the baudmode,
# using the baud formula this source documents in its own comments
# ("cBaudVal = (1000000 / cBaud) - 20", i.e. baud = 1000000/(baudmode+20)).
RE_SEROUT = re.compile(
    r'^(\s*)SerOut\s+([^\s,]+)\s*,\s*([^\s,]+)\s*,\s*\[\s*(.*?)\s*\]\s*$',
    re.IGNORECASE,
)
RE_SERIN = re.compile(
    r'^(\s*)SerIn\s+([^\s,]+)\s*,\s*([^\s,]+)\s*,\s*\[\s*(.*?)\s*\]\s*$',
    re.IGNORECASE,
)
RE_SERIN_TIMEOUT = re.compile(
    r'^(\s*)SerIn\s+([^\s,]+)\s*,\s*([^\s,]+)\s*,\s*([^\s,]+)\s*,\s*'
    r'([^\s,\[\]]+)\s*,\s*\[\s*(.*?)\s*\]\s*$',
    re.IGNORECASE,
)

RE_DELAYMS = re.compile(r'\bDelayMS\s+(\S+)', re.IGNORECASE)

RE_BINLIT = re.compile(r'%([01]{1,32})\b')

# BASIC hex literals ($FF, $FFFF, $1F80, ...) -> GCBASIC's 0x prefix.
RE_HEXLIT = re.compile(r'\$([0-9A-Fa-f]+)\b')

# C-header-style bitfield access (REGISTERbits_FIELD, as generated by
# MPLAB XC8 headers, e.g. "INTCONbits_GIE") -> GCBASIC's REGISTER.FIELD
# dot-bit syntax. General-purpose - applies to any identifier containing
# "bits_", not just a specific register.
RE_BITFIELD = re.compile(r'\b([A-Za-z_]\w*?)bits_([A-Za-z_]\w*)\b')

# CRead <expr> -> HEFReadWord( <expr> ). Not in COMMAND_LOOKUP because
# CRead is an inline expression, not a whole-line construct - it can
# appear more than once in a single statement (e.g. inside an If/And),
# so it needs its own expression-aware substitution rather than a
# single line-anchored match. Two shapes: already-parenthesized
# "CRead (expr)", and bare "CRead expr" where expr is a plain additive
# expression (identifier, optionally +/- a number/identifier) that ends
# at the next operator/keyword/comma/end-of-line.
RE_CREAD = re.compile(
    r'\bCRead\s*\(\s*([^()]*?)\s*\)'
    r'|\bCRead\s+((?:\w+\s*[+\-]\s*)*\w+)',
    re.IGNORECASE,
)


def _replace_cread(m):
    expr = m.group(1) if m.group(1) is not None else m.group(2)
    return f"HEFReadWord( {expr} )"

RE_DIM_LINE = re.compile(r"^\s*Dim\s+(.+?)\s+As\s+(\w+)\b", re.IGNORECASE)
RE_DWORD_TYPE = re.compile(r'\bDword\b', re.IGNORECASE)

# BASIC allows a space between an array name and its size, e.g.
# "Dim HEF_Array [64] As Byte" - but the bracket itself is also wrong for
# GCBASIC: GCBASIC arrays are declared and indexed with parentheses,
# "Dim HEF_Array(64) As Byte" / "HEF_Array(i)", and reserves [ ] for type
# casts (see https://gcbasic.sourceforge.io/help/_arrays.html). Any
# "Dim NAME [SIZE] As TYPE" is treated as an array declaration, and every
# later NAME[index] use of that same array is rewritten to NAME(index) -
# see find_array_declarations() / apply_array_bracket_to_paren().
RE_DIM_ARRAY_BRACKET = re.compile(r'^\s*Dim\s+(\w+)\s*\[', re.IGNORECASE)

# After the bracket->paren rewrite above, flag (and strip) any trailing
# word after "Dim NAME(...) As TYPE" that isn't a documented GCBASIC Dim
# modifier (Alias / At / = initialvalue) - e.g. the BASIC
# "Heap" qualifier, which has no GCBASIC equivalent and is not guessed at.
RE_DIM_TRAILING_TOKEN = re.compile(
    r'^(\s*Dim\s+\w+\(\s*[^)]*?\s*\)\s*As\s+\w+)\s+(\S.*?)\s*$', re.IGNORECASE
)
RE_DIM_RECOGNIZED_TRAILING = re.compile(r'^(Alias\b|At\b|=)', re.IGNORECASE)

# BASIC's "For var = start UpTo end" is GCBASIC's "For var = start To
# end" - GCBASIC has no UpTo/DownTo keywords.
RE_FOR_UPTO = re.compile(r'\bUpTo\b', re.IGNORECASE)

# A single-line "Repeat : Until cond" can't become a single-line
# "Do : Until cond" in GCBASIC (the exit condition has to be on its own
# "Loop Until" line) - handled as its own case before the generic
# Repeat/Until word substitutions below, which only handle a standalone
# "Until ..." line closing a multi-line block.
RE_REPEAT_UNTIL_ONELINE = re.compile(
    r'^(\s*)Repeat\s*:\s*Until\s+(.+?)\s*$', re.IGNORECASE
)

RE_EREAD_ASSIGN = re.compile(
    r'^(\s*)(\w+)\s*=\s*ERead\s+(.+?)\s*$', re.IGNORECASE
)
RE_EWRITE_BRACKET = re.compile(
    r'^(\s*)EWrite\s+([^,]+?)\s*,\s*\[\s*(\w+)\s*\]\s*$', re.IGNORECASE
)

RE_CLEAR_BARE = re.compile(r"^(\s*)Clear\s+(\w+)\s*$", re.IGNORECASE)
RE_SET_BARE = re.compile(r"^(\s*)Set\s+(\w+)\s*$", re.IGNORECASE)
# Don't touch "Set X On"/"Set X Off" - those are already valid GCBASIC.
RE_SET_ONOFF = re.compile(r'^\s*Set\s+\w+\s+(On|Off)\b', re.IGNORECASE)

RE_WHILE = re.compile(r'(?<!Do )\bWhile\b', re.IGNORECASE)
RE_WEND = re.compile(r'\bWend\b', re.IGNORECASE)

RE_REPEAT = re.compile(r'\bRepeat\b', re.IGNORECASE)
RE_UNTIL_START = re.compile(r'^(\s*)Until\b', re.IGNORECASE)

RE_INC = re.compile(r'\bInc\s+(\w+)\b', re.IGNORECASE)
RE_DEC = re.compile(r'\bDec\s+(\w+)\b', re.IGNORECASE)

RE_SELECT_CASE = re.compile(r'^\s*Select\s+Case\s+(.+?)\s*$', re.IGNORECASE)
RE_ELSE_IF = re.compile(r'\bElse\s*If\b', re.IGNORECASE)
RE_CASE_REL = re.compile(r'\bCase\s*([<>])\s*(\S+)', re.IGNORECASE)

# GCBASIC treats these as built-in math/logic operators (X Mod Y, A And B,
# Not X, A Or B, A Xor B). A BASIC Symbol constant that happens to be
# named one of these would collide with the operator in GCBASIC, so any
# such Symbol gets renamed with this prefix, and every use of it in the
# source is rewritten to match - see find_reserved_symbol_renames() /
# apply_reserved_renames().
RESERVED_MATH_WORDS = {'MOD', 'NOT', 'AND', 'OR', 'XOR'}
RESERVED_RENAME_PREFIX = 'CONVERT_ADAPTED_'

RE_DECLARE_LCD = re.compile(r'^(\s*)Declare\s+(LCD_\w+)\s+(.+?)\s*$', re.IGNORECASE)

RE_COMMENT_LINE = re.compile(r"^\s*['#;]")  # apostrophe / semicolon / directive

# BASIC Pro Pascal-style block comments: (* ... *). GCBASIC has no
# block-comment operator, only a leading ' for a same-line comment, so
# these get rewritten line-by-line - see convert_block_comment_line().
RE_BLOCK_COMMENT_START = re.compile(r'\(\*')
RE_BLOCK_COMMENT_END = re.compile(r'\*\)')


def split_code_comment(code_line):
    """Split a line (without its trailing newline) into (code, comment),
    where comment includes the leading apostrophe, e.g.
        'foo = 1   bar' -> ("foo = 1   ", "'bar")
    A double-quoted string is respected so an apostrophe inside "..." does
    not get treated as the start of a comment."""
    in_quotes = False
    for i, ch in enumerate(code_line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "'" and not in_quotes:
            return code_line[:i], code_line[i:]
    return code_line, ""

HELPER_SUBS = '''
' ---------------------------------------------------------------
' Auto-generated helper subs: GCBASIC's EPWrite/EPRead handle one
' EEPROM byte at a time, unlike BASIC-style ERead/EWrite which
' can pack/unpack a whole DWORD in one call. These subs replicate
' that behaviour for 32-bit (Long) variables using plain arithmetic
' (Mod / integer division) rather than shift operators, for the
' widest possible compatibility across GCBASIC versions.
' ---------------------------------------------------------------
Sub EE_WriteLong(location As Byte, longval As Long)
    Dim b0, b1, b2, b3 As Byte
    b0 = longval Mod 256
    b1 = (longval / 256) Mod 256
    b2 = (longval / 65536) Mod 256
    b3 = (longval / 16777216) Mod 256
    EPWrite location, b0
    EPWrite location + 1, b1
    EPWrite location + 2, b2
    EPWrite location + 3, b3
End Sub

Sub EE_ReadLong(location As Byte, Out longval As Long)
    Dim b0, b1, b2, b3 As Byte
    EPRead location, b0
    EPRead location + 1, b1
    EPRead location + 2, b2
    EPRead location + 3, b3
    longval = b0 + (b1 * 256) + (b2 * 65536) + (b3 * 16777216)
End Sub
'''

CONVERTER_VERSION = "1.0.0-build.1"
CONVERTER_DATE = "2026-08-07"
CONVERTER_BUILD = "1617"

# Derive the runtime OS string from the current platform so this stays correct
# for 32-bit, 64-bit, and non-Windows builds.
if platform.system() == "Windows":
    os_bits = "64 bit" if sys.maxsize > 2**31 else "32 bit"
    CONVERTER_OS = f"Windows {os_bits}"
else:
    CONVERTER_OS = platform.system()

HEADER_TEMPLATE = """'****************************************************************
'*  GENERAL BASIC to GCBASIC Converted ({date} ({os}) : Build {build}) *
'*  Source file : {source_name}
'*                                                                *
'*  This is a HEURISTIC, line-based conversion. It has NOT been  *
'*  compiled or tested. Search for '>>> REVIEW:' comments below  *
'*  for anything the converter was not confident about, and      *
'*  compile with the real GCBASIC toolchain before flashing.     *
'****************************************************************

"""


def split_dim_names(names_part):
    """'a, b, c' -> ['a', 'b', 'c'] (strips whitespace)."""
    return [n.strip() for n in names_part.split(',') if n.strip()]


def get_case_range_bounds(var_name, var_types):
    """Return the upper/lower bounds for a Select Case range based on the
    case variable's declared type, if known."""
    vtype = var_types.get(var_name.lower(), 'long')
    vtype = vtype.lower()
    if vtype in ('byte', 'bit'):
        return '0xFF', '0x00'
    if vtype in ('word', 'integer', 'short', 'ushort', 'unsigned int', 'unsigned short'):
        return '0xFFFF', '0x0000'
    if vtype in ('dword', 'long', 'ulong', 'unsigned long'):
        return '0xFFFFFFFF', '0x00000000'
    return '0xFFFFFFFF', '0x00000000'


def build_type_map(lines):
    """First pass: scan Dim declarations to know each variable's type,
    so ERead/EWrite can be routed to EPRead/EPWrite (Byte) or the
    EE_ReadLong/EE_WriteLong helpers (Long/Dword)."""
    types = {}
    for line in lines:
        m = RE_DIM_LINE.match(line)
        if m:
            names_part, type_part = m.groups()
            for name in split_dim_names(names_part):
                types[name.lower()] = type_part.lower()
    return types


def convert_line(line, var_types, needs_helpers, lookup_usage, select_case_var=None):
    """Apply all single-line regex transforms. Returns the transformed
    line (str). May set needs_helpers[0] = True as a side effect if a
    Long/Dword ERead/EWrite is found."""

    # Preserve the line ending exactly (may be '\n', '\r\n', or '' at EOF).
    stripped_end = line
    newline = ""
    for eol in ("\r\n", "\n", "\r"):
        if stripped_end.endswith(eol):
            newline = eol
            stripped_end = stripped_end[: -len(eol)]
            break
    code_line = stripped_end

    if code_line.strip() == "":
        return line

    # A line that IS entirely a comment (or a directive) - pass through.
    if RE_COMMENT_LINE.match(code_line):
        return line

    # Split off any trailing comment so regex substitutions never touch it.
    code, comment = split_code_comment(code_line)

    # BASIC hex literals ($FF, $1F80, ...) -> GCBASIC's 0x prefix.
    # Done here, right after the code/comment split, and also folded back
    # into code_line so the few special cases below that still match
    # against the whole code_line (Declare LCD_xxx, Repeat/Until, Dim
    # trailing-token) see converted 0x literals too.
    code = RE_HEXLIT.sub(r'0x\1', code)
    code_line = code + comment
    # automatically sees already-converted 0x literals.
    code = RE_HEXLIT.sub(r'0x\1', code)

    # Declare LCD_xxx -> comment out, flag for review (uses the whole
    # original code_line, since we're replacing the entire statement).
    m = RE_DECLARE_LCD.match(code_line)
    if m:
        indent, name, rest = m.groups()
        return (f"{indent}' >>> REVIEW: removed 'Declare {name} {rest}' - "
                f"no LCD library is used by this converter; re-add if needed{newline}")

    # Single-line "Repeat : Until cond" -> GCBASIC needs the exit
    # condition on its own "Loop Until" line, so this expands to two
    # lines rather than the generic word-for-word Repeat/Until subs below.
    m = RE_REPEAT_UNTIL_ONELINE.match(code_line)
    if m:
        indent, cond = m.groups()
        return f"{indent}Do{newline}{indent}Loop Until {cond}{newline}"

    # Dim NAME(SIZE) As TYPE <trailing> - flag/strip any trailing word
    # that isn't a recognized GCBASIC Dim modifier (Alias/At/=), e.g. the
    # BASIC "Heap" qualifier, which has no known GCBASIC
    # equivalent and is not guessed at.
    m = RE_DIM_TRAILING_TOKEN.match(code_line)
    if m:
        core, trailing = m.groups()
        if not RE_DIM_RECOGNIZED_TRAILING.match(trailing):
            return (f"{core}  ' >>> REVIEW: removed trailing '{trailing}' "
                     f"from this Dim - not a recognized GCBASIC Dim option "
                     f"(Alias/At/=), and its intended purpose in the "
                     f"original source is unclear; add the correct "
                     f"GCBASIC equivalent (e.g. 'At location') if needed"
                     f"{newline}")

    # Symbol alias -> #define
    m = RE_SYMBOL.match(code)
    if m:
        indent, name, value = m.groups()
        return f"{indent}#define {name} {value}{('  ' + comment) if comment else ''}{newline}"

    # Declare NAME = VALUE / Declare NAME VALUE -> #define NAME VALUE (a
    # compile-time constant assignment, same as Symbol above). Other
    # Declare forms (LCD_xxx) are handled separately, above.
    m = RE_DECLARE_ASSIGN.match(code)
    if m:
        indent, name, value = m.groups()
        return f"{indent}#define {name} {value}{('  ' + comment) if comment else ''}{newline}"

    # Config1..Config7 (PIC18F/enhanced-core numbered fuse words) all map
    # to GCBASIC's single #config directive - drop the trailing number.
    m = RE_CONFIG_LINE.match(code)
    if m:
        indent, rest = m.groups()
        return f"{indent}#Config{rest}{('  ' + comment) if comment else ''}{newline}"

    # Command lookup table (bit-alias Dim, Asm/EndAsm, CErase/CWrite,
    # and anything else added to COMMAND_LOOKUP later) - see the table
    # definition above for how to add new entries.
    lookup_result = apply_command_lookup(code, comment, newline, lookup_usage)
    if lookup_result is not None:
        return lookup_result

    # HSerOut [a, b, ...] -> HSerSend a / HSerSend b / ... (GCBASIC has no
    # bracketed item-list form - one call per item).
    m = RE_HSEROUT.match(code)
    if m:
        indent, items_str = m.groups()
        items = [it.strip() for it in items_str.split(',') if it.strip()]
        return ''.join(
            f"{indent}HSerSend {item}{('  ' + comment) if (comment and i == 0) else ''}{newline}"
            for i, item in enumerate(items)
        )

    # HSerIn timeout,label,[a, b] -> a = HSerReceive / If a = 255 Then
    # GoTo label / ... - HSerReceive's non-blocking "no new data" return
    # (255) is the closest available stand-in for BASIC's per-byte
    # timeout; flagged since it isn't exact (255 can also be genuine data,
    # and the original per-byte wait time isn't reproduced).
    m = RE_HSERIN.match(code)
    if m:
        indent, _timeout, label, items_str = m.groups()
        items = [it.strip() for it in items_str.split(',') if it.strip()]
        out = [f"{indent}' >>> REVIEW: approximated HSerIn's per-byte "
               f"timeout - GCBASIC's HSerReceive has no built-in timeout, "
               f"so this reads once and treats the non-blocking \"no new "
               f"data\" return (255) as a timeout; 255 can also be a "
               f"genuine received byte, and the original per-byte wait "
               f"is not reproduced - verify against {label}'s intended "
               f"behaviour{('  ' + comment) if comment else ''}{newline}"]
        for item in items:
            out.append(f"{indent}{item} = HSerReceive{newline}")
            out.append(f"{indent}If {item} = 255 Then GoTo {label}{newline}")
        return ''.join(out)

    # SerOut pin,baudmode,[a, b, ...] -> Ser1Send a / Ser1Send b / ...
    # (config constants for the Ser1 channel are emitted once in the
    # header - see find_serial_config()).
    m = RE_SEROUT.match(code)
    if m:
        indent, _pin, _baud, items_str = m.groups()
        items = [it.strip() for it in items_str.split(',') if it.strip()]
        return ''.join(
            f"{indent}Ser1Send {item}{('  ' + comment) if (comment and i == 0) else ''}{newline}"
            for i, item in enumerate(items)
        )

    # SerIn pin,baudmode,timeout,label,[a, b] -> GCBASIC's Ser1Receive has
    # no timeout/label mechanism (it blocks waiting for the start bit), so
    # this can't be approximated the way HSerIn's 255-sentinel was - flag
    # it and fall back to plain (untimed) Ser1Receive assignments.
    m = RE_SERIN_TIMEOUT.match(code)
    if m:
        indent, _pin, _baud, _timeout, label, items_str = m.groups()
        items = [it.strip() for it in items_str.split(',') if it.strip()]
        out = [f"{indent}' >>> REVIEW: SerIn's timeout/{label} has no "
               f"GCBASIC equivalent - Ser1Receive blocks waiting for the "
               f"start bit with no built-in timeout; implement the "
               f"desired timeout manually if one is needed"
               f"{('  ' + comment) if comment else ''}{newline}"]
        for item in items:
            out.append(f"{indent}{item} = Ser1Receive{newline}")
        return ''.join(out)

    # SerIn pin,baudmode,[a, b, ...] (no timeout) -> a = Ser1Receive / ...
    m = RE_SERIN.match(code)
    if m:
        indent, _pin, _baud, items_str = m.groups()
        items = [it.strip() for it in items_str.split(',') if it.strip()]
        return ''.join(
            f"{indent}{item} = Ser1Receive{('  ' + comment) if (comment and i == 0) else ''}{newline}"
            for i, item in enumerate(items)
        )

    # ERead assignment: var = ERead addr
    m = RE_EREAD_ASSIGN.match(code)
    if m:
        indent, var, addr = m.groups()
        addr = addr.strip()
        vtype = var_types.get(var.lower(), 'byte')
        tail = f"  {comment}" if comment else ""
        if vtype in ('dword', 'long'):
            needs_helpers[0] = True
            return f"{indent}EE_ReadLong {addr}, {var}{tail}{newline}"
        else:
            return f"{indent}EPRead {addr}, {var}{tail}{newline}"

    # EWrite addr, [var]
    m = RE_EWRITE_BRACKET.match(code)
    if m:
        indent, addr, var = m.groups()
        addr = addr.strip()
        vtype = var_types.get(var.lower(), 'byte')
        tail = f"  {comment}" if comment else ""
        if vtype in ('dword', 'long'):
            needs_helpers[0] = True
            return f"{indent}EE_WriteLong {addr}, {var}{tail}{newline}"
        else:
            return f"{indent}EPWrite {addr}, {var}{tail}{newline}"

    # Clear name  /  Set name   (bare bit form only - not "Set X On/Off",
    # which is already valid GCBASIC and is left untouched)
    if not RE_SET_ONOFF.match(code):
        m = RE_CLEAR_BARE.match(code)
        if m:
            indent, name = m.groups()
            tail = f"  {comment}" if comment else ""
            return f"{indent}{name} = 0{tail}{newline}"
        m = RE_SET_BARE.match(code)
        if m:
            indent, name = m.groups()
            tail = f"  {comment}" if comment else ""
            return f"{indent}{name} = 1{tail}{newline}"

    # Everything else: apply the remaining transforms to the code part only.
    code = RE_DELAYMS.sub(r'Wait \1 ms', code)
    code = RE_BINLIT.sub(r'0b\1', code)
    code, bitfield_count = RE_BITFIELD.subn(r'\1.\2', code)
    if bitfield_count:
        key = "REGISTERbits_FIELD -> REGISTER.FIELD"
        lookup_usage[key] = lookup_usage.get(key, 0) + bitfield_count
    code, cread_count = RE_CREAD.subn(_replace_cread, code)
    if cread_count:
        key = "CRead -> HEFReadWord( )"
        lookup_usage[key] = lookup_usage.get(key, 0) + cread_count
    code = RE_DWORD_TYPE.sub('Long', code)
    code = RE_INC.sub(r'\1++', code)
    code = RE_DEC.sub(r'\1--', code)
    code = RE_FOR_UPTO.sub('To', code)
    code = RE_WHILE.sub('Do While', code)
    code = RE_WEND.sub('Loop', code)
    code = RE_REPEAT.sub('Do', code)
    # A standalone "Until ..." line (the closing line of a former
    # Repeat...Until block) must become "Loop Until ..." in GCBASIC -
    # the exit condition belongs on the Loop line, not on its own.
    code = RE_UNTIL_START.sub(r'\1Loop Until', code)
    code = RE_ELSE_IF.sub('Else If', code)

    def case_repl(match):
        op = match.group(1)
        value = match.group(2).strip()
        if op == '>':
            upper, _ = get_case_range_bounds(select_case_var, var_types) if select_case_var else ('0xFFFFFFFF', '0x00000000')
            return f'Case {value} To {upper}'
        if op == '<':
            _, lower = get_case_range_bounds(select_case_var, var_types) if select_case_var else ('0xFFFFFFFF', '0x00000000')
            return f'Case {lower} To {value} - 1'
        return match.group(0)

    code = RE_CASE_REL.sub(case_repl, code)

    return code + comment + newline


def convert_block_comment_line(line, in_block_comment):
    """Handle one line that opens, continues, or closes a BASIC
    Pascal-style (* ... *) block comment. GCBASIC has no block-comment
    syntax - only a leading ' for a same-line comment - so every line
    that falls inside such a block is rewritten as a ' comment line,
    with the (* / *) delimiters themselves stripped out.

    If a (* or *) delimiter shares a physical line with real code that
    can't be safely split onto its own line, the whole line is
    commented out and flagged with '>>> REVIEW:' instead of guessed at,
    since silently leaving that code active (or silently dropping it)
    could change program behaviour.

    Returns (new_line, still_in_block_comment)."""
    stripped_end = line
    newline = ""
    for eol in ("\r\n", "\n", "\r"):
        if stripped_end.endswith(eol):
            newline = eol
            stripped_end = stripped_end[: -len(eol)]
            break
    body = stripped_end

    def review(reason_line):
        return (f"' >>> REVIEW: line commented out - BASIC (* *) block "
                f"comment shares a line with code here: "
                f"{reason_line.strip()}{newline}")

    if not in_block_comment:
        start = RE_BLOCK_COMMENT_START.search(body)
        before, after = body[:start.start()], body[start.end():]
        end = RE_BLOCK_COMMENT_END.search(after)
        if end is None:
            # Block comment opens here and continues onto later lines.
            if before.strip():
                return review(body), True
            return before + "'" + after + newline, True
        # Both (* and *) close on this same line.
        inner, rest = after[: end.start()], after[end.end():]
        if before.strip() or rest.strip():
            return review(body), False
        return before + "'" + inner + newline, False
    else:
        end = RE_BLOCK_COMMENT_END.search(body)
        if end is None:
            # Still inside the block comment for the whole line.
            if body.strip():
                return "'" + body + newline, True
            return body + newline, True
        # Block comment closes partway through this line.
        inner, rest = body[: end.start()], body[end.end():]
        if rest.strip():
            return review(body), False
        return "'" + inner + newline, False


def find_reserved_symbol_renames(lines):
    """Scan for 'Symbol NAME = value' declarations whose NAME collides
    with a GCBASIC reserved math/logic operator (Mod/Not/And/Or/Xor).
    Returns {exact_declared_name: renamed_name}, preserving the exact
    casing used in the source so the later whole-word rename pass only
    touches genuine references to that constant, not unrelated uses of
    the operator keyword itself."""
    renames = {}
    for line in lines:
        stripped_end = line
        for eol in ("\r\n", "\n", "\r"):
            if stripped_end.endswith(eol):
                stripped_end = stripped_end[: -len(eol)]
                break
        code, _ = split_code_comment(stripped_end)
        m = RE_SYMBOL.match(code)
        if m:
            _, name, _ = m.groups()
            if name.upper() in RESERVED_MATH_WORDS and name not in renames:
                renames[name] = RESERVED_RENAME_PREFIX + name
    return renames


def apply_reserved_renames(lines, renames):
    """Whole-word replace every use of each reserved-colliding Symbol
    name with its CONVERT_ADAPTED_-prefixed replacement, throughout the
    code portion of every line (comments left untouched). Runs before
    the main line-by-line conversion so the later Symbol -> #define
    step, and every other reference to the constant, already sees the
    renamed identifier."""
    if not renames:
        return lines
    patterns = [
        (new_name, re.compile(r'\b' + re.escape(old_name) + r'\b'))
        for old_name, new_name in renames.items()
    ]
    result = []
    for line in lines:
        stripped_end = line
        newline = ""
        for eol in ("\r\n", "\n", "\r"):
            if stripped_end.endswith(eol):
                newline = eol
                stripped_end = stripped_end[: -len(eol)]
                break
        code, comment = split_code_comment(stripped_end)
        for new_name, pattern in patterns:
            code = pattern.sub(new_name, code)
        result.append(code + comment + newline)
    return result


def find_array_declarations(lines):
    """Scan for BASIC-style array declarations: 'Dim NAME [SIZE] As
    TYPE' (with or without a space before the bracket). Returns the set
    of array names found (original casing), so every NAME[...] use of
    them elsewhere in the file can be rewritten from bracket to paren
    indexing to match GCBASIC's array syntax."""
    names = set()
    for line in lines:
        stripped_end = line
        for eol in ("\r\n", "\n", "\r"):
            if stripped_end.endswith(eol):
                stripped_end = stripped_end[: -len(eol)]
                break
        code, _ = split_code_comment(stripped_end)
        m = RE_DIM_ARRAY_BRACKET.match(code)
        if m:
            names.add(m.group(1))
    return names


def apply_array_bracket_to_paren(lines, array_names):
    """Rewrite every NAME[expr] use of a known array (declarations and
    later indexing alike, since both share the same 'NAME[...]' shape)
    to GCBASIC's NAME(expr) form. Runs before the main line-by-line
    conversion, on the code portion of every line only."""
    if not array_names:
        return lines
    patterns = [
        (name, re.compile(r'\b' + re.escape(name) + r'\s*\[([^\[\]]*)\]'))
        for name in array_names
    ]
    result = []
    for line in lines:
        stripped_end = line
        newline = ""
        for eol in ("\r\n", "\n", "\r"):
            if stripped_end.endswith(eol):
                newline = eol
                stripped_end = stripped_end[: -len(eol)]
                break
        code, comment = split_code_comment(stripped_end)
        for name, pattern in patterns:
            code = pattern.sub(
                lambda m, n=name: f"{n}({m.group(1).strip()})", code
            )
        result.append(code + comment + newline)
    return result


def find_constant_values(lines):
    """Scan for 'Symbol NAME = VALUE' declarations (after the reserved-word
    rename pass) and return {name: value}, so a pin symbol used in a
    SerOut/SerIn call (e.g. CONVERT_ADAPTED_MOD -> "LATA.4") can be
    resolved back to a port/pin for GCBASIC's SER1_TXPORT/SER1_TXPIN."""
    values = {}
    for line in lines:
        stripped_end = line
        for eol in ("\r\n", "\n", "\r"):
            if stripped_end.endswith(eol):
                stripped_end = stripped_end[: -len(eol)]
                break
        code, _ = split_code_comment(stripped_end)
        m = RE_SYMBOL.match(code)
        if m:
            _, name, value = m.groups()
            values[name] = value.strip()
    return values


def split_port_pin(value):
    """Split a "PORTx.n" / "LATx.n" style value into (port, pin), or
    (None, None) if it doesn't look like that shape."""
    m = re.match(r'^(\w+)\.(\w+)$', value.strip())
    if m:
        return m.group(1), m.group(2)
    return None, None


def parse_picbasic_int(token):
    """Parse a BASIC integer literal: plain decimal, $hex, or %binary."""
    token = token.strip()
    try:
        if token.startswith('$'):
            return int(token[1:], 16)
        if token.startswith('%'):
            return int(token[1:], 2)
        return int(token, 10)
    except ValueError:
        return None


def picbasic_baudmode_to_bps(token):
    """Convert a BASIC SerOut/SerIn baudmode value to bps, using the
    formula this source documents in its own comments (cBaudVal =
    (1000000 / cBaud) - 20), i.e. baud = 1000000 / (baudmode + 20)."""
    baudmode = parse_picbasic_int(token)
    if baudmode is None or baudmode <= -20:
        return None
    return round(1000000 / (baudmode + 20))


def find_serial_config(lines, symbol_values):
    """Scan for hardware-USART usage (HSerOut/HSerIn/HSerSend/HSerReceive)
    and the first software-serial SerOut/SerIn call, resolving the pin
    symbol and baudmode for each. Returns
    (hw_used, sw_tx(port, pin, bps) or None, sw_rx(port, pin, bps) or None)."""
    hw_used = False
    sw_tx = None
    sw_rx = None
    for line in lines:
        stripped_end = line
        for eol in ("\r\n", "\n", "\r"):
            if stripped_end.endswith(eol):
                stripped_end = stripped_end[: -len(eol)]
                break
        code, _ = split_code_comment(stripped_end)
        if re.search(r'\bHSer(Out|In|Send|Receive)\b', code, re.IGNORECASE):
            hw_used = True
        if sw_tx is None:
            m = RE_SEROUT.match(code)
            if m:
                _, pin_token, baud_token, _ = m.groups()
                port, pin = split_port_pin(symbol_values.get(pin_token, pin_token))
                bps = picbasic_baudmode_to_bps(baud_token)
                sw_tx = (port, pin, bps)
        if sw_rx is None:
            m = RE_SERIN_TIMEOUT.match(code) or RE_SERIN.match(code)
            if m:
                groups = m.groups()
                pin_token, baud_token = groups[1], groups[2]
                port, pin = split_port_pin(symbol_values.get(pin_token, pin_token))
                bps = picbasic_baudmode_to_bps(baud_token)
                sw_rx = (port, pin, bps)
    return hw_used, sw_tx, sw_rx


def convert_source(text, source_name, fallback_xtal=None):
    lines = text.splitlines(keepends=True)

    # Rename any Symbol constant whose name collides with a GCBASIC
    # reserved math/logic operator (Mod/Not/And/Or/Xor), and update
    # every use of it in the source, before anything else runs.
    reserved_renames = find_reserved_symbol_renames(lines)
    if reserved_renames:
        lines = apply_reserved_renames(lines, reserved_renames)

    # Rewrite BASIC-style "Dim NAME [SIZE] As TYPE" arrays and every
    # NAME[index] use of them to GCBASIC's NAME(SIZE) / NAME(index) form.
    array_names = find_array_declarations(lines)
    if array_names:
        lines = apply_array_bracket_to_paren(lines, array_names)

    # Resolve the software/hardware serial config needed for any
    # SerOut/SerIn/HSerOut/HSerIn calls in this file (pin, baud, etc.).
    symbol_values = find_constant_values(lines)
    hw_serial_used, sw_serial_tx, sw_serial_rx = find_serial_config(lines, symbol_values)

    # If HEF (Hybrid EEPROM/Flash) memory is referenced anywhere, the chip
    # may not define ChipHEFMemWords directly - add the fallback #script
    # block so it's derived from ChipSAFMemWords instead.
    hef_used = any(re.search(r'HEF', line, re.IGNORECASE) for line in lines)

    var_types = build_type_map(lines)
    needs_helpers = [False]
    lookup_usage = {}

    out_lines = []
    chip_inserted = False
    device_val = None
    xtal_val = None

    # First scan for Device=/Xtal= so we can merge them into one #chip line
    # at the position of the first one encountered.
    for line in lines:
        m = RE_DEVICE.match(line)
        if m:
            device_val = m.group(1)
        m = RE_XTAL.match(line)
        if m:
            xtal_val = m.group(1)

    select_case_var = None
    in_block_comment = False
    for line in lines:
        if in_block_comment or RE_BLOCK_COMMENT_START.search(line):
            converted_line, in_block_comment = convert_block_comment_line(
                line, in_block_comment
            )
            out_lines.append(converted_line)
            continue

        if RE_DEVICE.match(line):
            if not chip_inserted:
                speed = xtal_val or fallback_xtal
                if device_val and speed:
                    # Convert Device= to #chip, but comment the line out with \
                    out_lines.append(f"//#chip {device_val}, {speed}\n")
                elif device_val:
                    out_lines.append(f"//#chip {device_val}\n")
                    out_lines.append(
                        "' >>> REVIEW: no Xtal= found - set clock speed "
                        "above, e.g. '#chip {}, 20'\n".format(device_val)
                    )
                chip_inserted = True
            continue  # drop the original Device= line either way
        if RE_XTAL.match(line):
            continue  # already folded into #chip line above

        code, _ = split_code_comment(line)
        m = RE_SELECT_CASE.match(code)
        if m:
            expr = m.group(1).strip()
            ident_match = re.match(r'([A-Za-z_][A-Za-z0-9_]*)', expr)
            select_case_var = ident_match.group(1) if ident_match else expr
        elif code.strip().upper() == 'END SELECT':
            select_case_var = None

        out_lines.append(convert_line(line, var_types, needs_helpers, lookup_usage, select_case_var=select_case_var))

    body = ''.join(out_lines)
    header = HEADER_TEMPLATE.format(
        source_name=source_name,
        date=CONVERTER_DATE,
        os=CONVERTER_OS,
        build=CONVERTER_BUILD,
    )
    if reserved_renames:
        header += (
            "' NOTE: the following Symbol constant(s) were renamed because\n"
            "' their BASIC name collides with a GCBASIC reserved\n"
            "' math/logic operator - every use in this file was updated:\n"
        )
        for old_name, new_name in reserved_renames.items():
            header += f"'   {old_name}  ->  {new_name}\n"
        header += "\n"

    if hef_used:
        header += (
            "'HEF (Hybrid EEPROM/Flash) memory is used in this file - not\n"
            "'every chip defines ChipHEFMemWords directly, so fall back to\n"
            "'ChipSAFMemWords when it's missing.\n"
            "#script\n"
            "    If NODEF(ChipHEFMemWords) Then\n"
            "        If DEF(ChipSAFMemWords) Then\n"
            "            ChipHEFMemWords = ChipSAFMemWords\n"
            "        End If\n"
            "    End If\n"
            "#endscript\n\n"
        )

    if sw_serial_tx or sw_serial_rx:
        header += (
            "'Software serial (Ser1) - enabling constants required by\n"
            "'GCBASIC's Ser1Send/Ser1Receive (see SerNSend/SerNReceive docs).\n"
            "'Baud derived from the original SerOut/SerIn baudmode using the\n"
            "'formula this source documents in its own comments: baud =\n"
            "'1000000 / (baudmode + 20) - verify against the target device.\n"
            "#include <SoftSerial.h>\n"
        )
        if sw_serial_tx:
            port, pin, bps = sw_serial_tx
            if bps is None:
                header += "' >>> REVIEW: could not derive SER1_BAUD from the SerOut baudmode - set it by hand\n"
            else:
                header += f"#define SER1_BAUD {bps}\n"
            if port and pin:
                header += f"#define SER1_TXPORT {port}\n#define SER1_TXPIN {pin}\n"
            else:
                header += "' >>> REVIEW: could not resolve the SerOut pin to a PORTx.n value - set SER1_TXPORT / SER1_TXPIN by hand\n"
        if sw_serial_rx:
            port, pin, bps = sw_serial_rx
            if sw_serial_tx is None and bps is not None:
                header += f"#define SER1_BAUD {bps}\n"
            if port and pin:
                header += f"#define SER1_RXPORT {port}\n#define SER1_RXPIN {pin}\n"
            else:
                header += "' >>> REVIEW: could not resolve the SerIn pin to a PORTx.n value - set SER1_RXPORT / SER1_RXPIN by hand\n"
        header += "\n"

    if hw_serial_used:
        header += (
            "'Hardware USART - enabling constants required by GCBASIC's\n"
            "'HSerSend/HSerReceive (see RS232 Hardware Overview / HSerSend docs).\n"
        )
        m = re.search(r'^([ \t]*#define\s+Hserial_Baud\b.*)$', body, re.IGNORECASE | re.MULTILINE)
        if m:
            # Insert right after the actual #define Hserial_Baud line, since
            # #define is a straight textual substitution and USART_BAUD_RATE
            # must reference Hserial_Baud only after it has been defined.
            insert_at = m.end()
            usart_block = (
                "\n#define USART_BAUD_RATE Hserial_Baud\n"
                "#define USART_TX_BLOCKING\n"
                "#define USART_DELAY OFF"
            )
            body = body[:insert_at] + usart_block + body[insert_at:]
            header += "'(inserted just after '#define Hserial_Baud' below)\n\n"
        else:
            header += (
                "' >>> REVIEW: no '#define Hserial_Baud ...' was found to "
                "anchor this to - add it manually after Hserial_Baud is "
                "defined, or replace Hserial_Baud below with a literal baud rate\n"
                "#define USART_BAUD_RATE Hserial_Baud\n"
                "#define USART_TX_BLOCKING\n"
                "#define USART_DELAY OFF\n\n"
            )

    review_count = body.count(">>> REVIEW:")
    result = header + body

    if needs_helpers[0]:
        result += HELPER_SUBS

    return result, review_count, lookup_usage


def main():
    parser = argparse.ArgumentParser(
        description="Convert a BASIC-style .bas source file to GCBASIC syntax."
    )
    parser.add_argument("source", help="Path to the source .bas file to convert")
    parser.add_argument(
        "-o", "--output",
        help="Output file path (default: source file's extension replaced with .GCB)"
    )
    parser.add_argument(
        "--xtal",
        help="Fallback clock speed (MHz) to use in the #chip line if the "
             "source has no 'Xtal = n' line",
        default=None,
    )
    args = parser.parse_args()

    src_path = Path(args.source)
    if not src_path.is_file():
        print(f"Error: source file not found: {src_path}", file=sys.stderr)
        sys.exit(1)

    text = src_path.read_text(encoding="utf-8", errors="replace")

    reserved_renames = find_reserved_symbol_renames(text.splitlines(keepends=True))
    converted, review_count, lookup_usage = convert_source(text, src_path.name, fallback_xtal=args.xtal)

    out_path = Path(args.output) if args.output else src_path.with_suffix(".GCB")
    out_path.write_text(converted, encoding="utf-8")

    print(f"Wrote {out_path}")
    for old_name, new_name in reserved_renames.items():
        print(f"Renamed reserved-word Symbol constant '{old_name}' -> "
              f"'{new_name}' (and all its uses) since '{old_name}' is a "
              f"GCBASIC operator.")
    for name, count in lookup_usage.items():
        plural = "s" if count != 1 else ""
        print(f"Converted {count} use{plural} of '{name}' (see COMMAND_LOOKUP).")
    if review_count:
        print(f"{review_count} line(s) flagged with '>>> REVIEW:' - please check them by hand.")
    print("Reminder: this is a heuristic converter. Compile the output with "
          "the real GCBASIC toolchain and review it before flashing to a chip.")


if __name__ == "__main__":
    main()
