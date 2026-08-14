# GenBasic to GCBASIC Converter

**File:** `genbasic_to_gcbasic.py`  
**Version:** 1.1.0 (Build 1621)  
**Date:** 2026-08-14

Heuristic source-to-source converter for porting **General BASIC** style code to **Great Cow BASIC (GCBASIC)**.

---

## Install location

Place these files in:

```
GCSTUDIO\GCBASIC\CONVERTER\
```

Required:

| File | Purpose |
|------|---------|
| `genbasic_to_gcbasic.py` | Converter script |
| `README.md` | This documentation |

Python 3 is required to run the converter.

---

## Usage

```text
python genbasic_to_gcbasic.py source.bas
python genbasic_to_gcbasic.py source.bas -o my_output.gcb
python genbasic_to_gcbasic.py source.bas --xtal 20
```

| Argument | Description |
|----------|-------------|
| `source` | Path to the General BASIC `.bas` / `.prt` source file |
| `-o`, `--output` | Output path (default: same name with `.GCB` extension) |
| `--xtal` | Fallback clock speed (MHz) if the source has no `Xtal = n` line |

---

## What it converts

- `Device` / `Xtal` → merged `#chip` directive
- `Symbol` / `Declare` assignments → `#define`
- `Config1` … `ConfigN` → `#config`
- `DelayMS` → `Wait n ms`
- `%binary` and `$hex` literals → `0b` / `0x` forms
- `Dword` → `Long`
- Bare `Clear` / `Set` → assignments to `0` / `1`
- `While` / `Wend` → `Do While` / `Loop`
- `Repeat` / `Until` → `Do` / `Loop Until`
- `Inc` / `Dec` → `++` / `--`
- `UpTo` → `To` (For loops)
- `Select Case` relational forms → `Case` ranges
- `GOSUB name` → bare `name` (Sub call)
- `label:` … `Return` (GOSUB targets only) → `Sub name` … `End Sub`
  - Intermediate `Return` → `Exit Sub`
  - Final `Return` absorbed into `End Sub`
  - Plain mid-sub labels (e.g. `CFG:`) stay as labels
- Serial: `HSerOut` / `SerOut` / `SerIn` helpers where safe
- Residual mid-line `HSerOut[...]` → `NOP // ...` with a `>>> REVIEW` marker
- `ERead` / `EWrite` → `EPRead` / `EPWrite` or Long helpers
- `CRead` → `HEFReadWord()`
- Array brackets `[ ]` → parentheses `( )`
- Reserved Symbol names (`Mod`, `And`, …) renamed to avoid GCBASIC operators
- Pascal-style `(* … *)` block comments → line comments

---

## Review markers

Anything the converter cannot safely rewrite is left with a marker:

```bas
' >>> REVIEW: description of the issue \original line
```

Also used:

```bas
NOP // original statement   ' neutralized so the file still compiles
```

**Always** search for `>>> REVIEW:` in the output, fix those items by hand, and compile with the real GCBASIC toolchain before flashing.

---

## Notes

- The converter is **heuristic and line-based**, not a full language parser.
- Output is **not** compiled or tested automatically.
- Helper routines (e.g. `ArrayToString`, EEPROM Long read/write) may be appended when needed.
- Software serial and hardware USART `#define` blocks are emitted when the source uses those features.

---

## Example

```text
python genbasic_to_gcbasic.py myprog.bas -o myprog.GCB --xtal 32
```

Then open `myprog.GCB` in GCStudio, resolve any `>>> REVIEW:` items, and compile.
