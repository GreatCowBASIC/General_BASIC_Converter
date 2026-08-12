# General BASIC to GCBASIC conversion summary

This document summarizes the current conversion workflow used by the General-Basic-to-GCBASIC translator (`genbasic_to_gcbasic.py`).

## Goal

The converter performs a best-effort, source-to-source translation from General Basic-Pro/Photon style syntax into GCBASIC syntax. It is designed to automate the repetitive mechanical changes that come up during a port, while leaving ambiguous or unsafe constructs as review markers instead of silently guessing.

## Usage

Source files must have the extension of PRT.  Therefore rename source files rename from BAS/TXT etc to  PRT.

```
Usage from within GCBASIC

    // This will convert and compile
    #chip 16F15355, 32
    #include "source.prt"
    #option Explicit
    // This will created an output file that is then included as a source.


From the command line
    python genbasic_to_gcbasic.py source.prt
    python genbasic_to_gcbasic.py source.prt -o my_output.gcb
    python genbasic_to_gcbasic.py source.prt --xtal 20   (fallback clock speed if no Xtal= line is found)
```

## Conversion strategy

1. **Pre-passes over the whole file**, before any line-by-line rewriting:
   - Rename any `Symbol` constant whose name collides with a GCBASIC reserved math/logic operator (`Mod`, `Not`, `And`, `Or`, `Xor`), and rewrite every use of it in the file to the renamed form (`CONVERT_ADAPTED_<name>`).
   - Detect General Basic-style array declarations (`Dim NAME [SIZE] As TYPE`) and rewrite the declaration plus every `NAME[index]` use elsewhere to GCBASIC's paren form (`NAME(SIZE)` / `NAME(index)`), since GCBASIC reserves `[ ]` for type casts, not arrays.
   - Resolve the software/hardware serial configuration needed by any `SerOut`/`SerIn`/`HSerOut`/`HSerIn` calls (pin, baud rate, port) so the right `#define`s can be emitted once, up front.
   - Detect whether HEF (Hybrid EEPROM/Flash) memory is referenced anywhere, to decide whether the `ChipHEFMemWords` fallback `#script` block is needed.

2. **Rewrite directives and declarations.**
   - `Device`/`Xtal` lines become a merged `#chip` directive.
   - `Symbol name = value` and `Declare name = value` / `Declare name value` both become `#define name value`.
   - `Config1`..`Config7` (PIC18F/enhanced-core numbered fuse words) all collapse to GCBASIC's single `#config` directive the trailing number is a General Basic-ism with no GCBASIC meaning and is dropped entirely.
   - `Declare LCD_xxx ...` is commented out and flagged for review (no LCD library is assumed).

3. **Translate literals, statements, and control-flow forms.**
   - `DelayMS n` -> `Wait n ms`.
   - `%binary` literals -> `0b...`.
   - `$hex` literals (any length: `$FF`, `$1F80`, `$FFFFFFFF`, ...) -> `0x...`. Applied once, early, right after the code/comment split, so every other rewrite path (Symbol, Declare, Config, the command lookup table, serial commands) automatically sees already-converted `0x` literals.
   - `Dim var As Dword` -> `Dim var As Long`.
   - `ERead`/`EWrite` -> `EPRead`/`EPWrite` (byte variables) or the generated `EE_ReadLong`/`EE_WriteLong` helper subs (Long/Dword variables).
   - Bare `Clear name` / `Set name` -> `name = 0` / `name = 1` (only the bare-bit form; `Set name On/Off` is left untouched since that's already valid GCBASIC).
   - `While ... Wend` -> `Do While ... Loop`.
   - `Repeat ... Until cond` -> `Do ... Loop Until cond`, including the single-line `Repeat : Until cond` form, which expands to a proper two-line `Do` / `Loop Until` pair.
   - `Inc var` / `Dec var` -> `var++` / `var--`.
   - `For i = start UpTo end` -> `For i = start To end` (GCBASIC has no `UpTo`/`DownTo`).
   - `Select Case` comparisons like `Case > n` / `Case < n` -> `Case n To <upper-bound>` / `Case <lower-bound> To n - 1`, with the bound inferred from the variable's declared type where possible; flagged for review when it can't be determined safely.

4. **Command lookup table** (`COMMAND_LOOKUP`) a single, easily-extended table for one-off statement-level substitutions that don't need bespoke logic. Two kinds of entries:
   - `"rewrite"` the match is replaced outright with new GCBASIC syntax.
   - `"comment_out"` the original line is kept but commented out with a `//!` marker, with a `//! use XXX` hint added on the line above, for constructs with no safe automatic 1:1 rewrite.

   Current entries:
   - `Dim x As y.n` (bit-alias declaration) -> `Dim x As Bit Alias y.n`.
   - `Asm` / `EndAsm` block markers -> `#asmraw` (used for both the open and close, since that's GCBASIC's raw-assembly wrapper).
   - `CErase` -> commented out with `//! use HEFEraseBlock`.
   - `CWrite` -> commented out with `//! use HEFWriteBlock`.

   Adding a new one-off replacement only requires a new entry in this table no other code needs to change. Each match increments a `lookup_usage` counter, so `main()` prints one summary line per construct actually found (not one per occurrence).

5. **Inline expression substitutions** (not in the lookup table, since these can occur more than once per statement e.g. inside an `If ... And ...` line rather than being anchored to the whole line):
   - `CRead expr` / `CRead (expr)` -> `HEFReadWord( expr )`. Handles both the bare-expression form (`CRead HEF_Address + 1`) and the already-parenthesized form.
   - `REGISTERbits_FIELD` (the MPLAB XC8 C-header bitfield naming convention, e.g. `INTCONbits_GIE`) -> `REGISTER.FIELD` (e.g. `INTCON.GIE`). General-purpose matches any identifier containing `bits_`, not a fixed list of registers.

   Both still report through the same `lookup_usage` counter/summary mechanism as the table above.

6. **Block comments.** Pascal-style `(* ... *)` block comments (General Basic's way of disabling a whole chunk of code, often spanning many lines) are rewritten line-by-line into GCBASIC `'` line comments, since GCBASIC has no block-comment operator. A line where a `(*`/`*)` delimiter shares space with real code that can't be safely split is commented out whole and flagged for review instead of guessed at.

7. **Serial commands.** GCBASIC has no General Basic-style bracketed item-list serial statements, so:
   - `HSerOut [a, b, ...]` -> one `HSerSend` call per item.
   - `HSerIn timeout,label,[a, b]` -> `a = HSerReceive` / `If a = 255 Then GoTo label`, per item. `HSerReceive`'s non-blocking "no new data" return (255) is the closest available stand-in for General Basic's per-byte timeout; flagged for review since it isn't exact (255 can also be a genuine received byte, and the original per-byte wait isn't reproduced).
   - `SerOut pin,baudmode,[a, b, ...]` -> one `Ser1Send` call per item.
   - `SerIn pin,baudmode,[a, b]` (no timeout) -> `a = Ser1Receive` per item. The timeout/label form is flagged for review instead, since `Ser1Receive` blocks waiting for the start bit with no built-in timeout mechanism.

   The necessary channel configuration is derived automatically and emitted once:
   - Software serial (`Ser1`): the pin symbol is traced back to its `Symbol`-declared value (e.g. `CONVERT_ADAPTED_MOD` -> `LATA.4`) to fill in `SER1_TXPORT`/`SER1_TXPIN` (and `SER1_RXPORT`/`SER1_RXPIN` if `SerIn` is used), and the baud rate is computed from the General Basic baudmode using the formula the source itself documents in its own comments (`cBaudVal = (1000000 / cBaud) - 20`, i.e. `baud = 1000000 / (baudmode + 20)`), plus `#include <SoftSerial.h>`.
   - Hardware USART: `#define USART_BAUD_RATE Hserial_Baud`, `#define USART_TX_BLOCKING`, `#define USART_DELAY OFF` are inserted immediately after the actual `#define Hserial_Baud ...` line in the body (not at the very top of the file), since `#define` is a straight textual substitution and `USART_BAUD_RATE` must reference `Hserial_Baud` only after it has been defined.

8. **HEF memory fallback script.** If `HEF` is referenced anywhere in the source (`HEF_Array`, `HEF_Address`, `HEFReadWord`, `HEFWriteBlock`, `HEFEraseBlock`, etc.), the following block is inserted once near the top of the header, since not every chip defines `ChipHEFMemWords` directly:
   ```gcbasic
   #script
       If NODEF(ChipHEFMemWords) Then
       warning 1
           If DEF(ChipSAFFMemWords) Then
               warning 2
               ChipHEFMemWords = ChipSAFMemWords
           End If
       End If
   #endscript
   ```

9. **Add review markers for anything questionable.** Lines that cannot be translated with high confidence are either commented out with a review note, or left in place with the note attached, so the output stays compilable enough to inspect while avoiding risky silent rewrites.

## Review-oriented behavior

The converter uses review markers such as:

```gcbasic
' >>> REVIEW: <reason>
```

These are emitted when:

- the transformation is only an approximation (e.g. the `HSerIn` timeout emulation),
- the original construct has no direct GCBASIC equivalent (e.g. `SerIn` with a timeout/label),
- the converter cannot safely infer the target type or range (e.g. an unbounded `Select Case >` comparison),
- a `Dim` has a trailing modifier that isn't a recognized GCBASIC option (e.g. General Basic-Photon's `Heap`) and its intended purpose isn't guessed at,
- or the syntax is too ambiguous to rewrite automatically (e.g. a `(* ... *)` delimiter sharing a line with real code).

`main()` also prints, after conversion:
- one line per reserved-word `Symbol` rename performed,
- one line per `COMMAND_LOOKUP`/inline-substitution construct actually found, with its occurrence count,
- a count of `'>>> REVIEW:` lines needing manual attention,
- a closing reminder to compile the output with the real GCBASIC toolchain before flashing it.

## Helper support

`EE_ReadLong`/`EE_WriteLong` helper subs are appended automatically to the output file if the source uses `ERead`/`EWrite` on a Long/Dword-typed variable, since GCBASIC's `EPRead`/`EPWrite` only handle one EEPROM byte at a time.

## Expected workflow

After conversion:

1. Review the generated source, especially anywhere marked `'>>> REVIEW:` or `//!`.
2. Inspect the one-line-per-construct summary printed to the console.
3. Compile the output with the real GCBASIC compiler.
4. Fix any remaining syntax or semantic issues manually in particular, the `HSerIn`/`SerIn` timeout approximations, and any Dim modifier flagged as unrecognized.

## Notes

This is not a full parser for any language. It is a line-oriented, best-effort translator meant to produce a strong first-pass conversion and keep risky transformations visible, not to replace manual review. The `COMMAND_LOOKUP` table is intended as the extension point for future one-off General Basic-construct-to-GCBASIC-hint mappings as they come up.

Enjoy