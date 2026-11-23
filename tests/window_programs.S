// Guest programs for test_window: EL0 loads and stores that go through the user-memory window.
// Each runs from name to name_end. x0: address; the rest as each says.
#ifdef __APPLE__
#define SYM(x) _##x
#else
#define SYM(x) x
#endif

.macro WPROG name
    .p2align 2
    .globl SYM(\name)
    .globl SYM(\name\()_end)
SYM(\name):
.endm
.macro WEND name
    b SYM(\name\()_end)
    .p2align 6
SYM(\name\()_end):
    nop
.endm

    .text
    .p2align 14
    .globl SYM(window_programs_begin)
SYM(window_programs_begin):

WPROG wrd                       // x1 = [x0]
    ldr x1, [x0]
WEND wrd

WPROG wwr                       // [x0] = x1
    str x1, [x0]
WEND wwr

WPROG wwr32                     // [x0] = w1 (an instruction word)
    str w1, [x0]
WEND wwr32

WPROG wpair                     // x1, x2 = [x0], [x0 + 8]; [x0 + 16], [x0 + 24] = x2, x1
    ldp x1, x2, [x0]
    stp x2, x1, [x0, #16]
WEND wpair

WPROG wrdsweep                  // x4 += [x0] for x2 pages, x3 apart
1:  ldr x5, [x0]
    add x4, x4, x5
    add x0, x0, x3
    subs x2, x2, #1
    b.ne 1b
WEND wrdsweep

WPROG wwrsweep                  // [x0] = x1, x1 += 1, for x2 pages, x3 apart
1:  str x1, [x0]
    add x1, x1, #1
    add x0, x0, x3
    subs x2, x2, #1
    b.ne 1b
WEND wwrsweep

WPROG wwr32sweep                // [x0] = w1 for x2 pages, x3 apart
1:  str w1, [x0]
    add x0, x0, x3
    subs x2, x2, #1
    b.ne 1b
WEND wwr32sweep

WPROG wvsweep                   // a SIMD load from x2 pages, x3 apart: not a windowed access, so only the TLB learns the pages
1:  ld1 {v0.16b}, [x0]
    add x0, x0, x3
    subs x2, x2, #1
    b.ne 1b
WEND wvsweep

WPROG wrdloop                   // x2 reads of [x0]: x4 |= each, x6 &= each
1:  ldr x5, [x0]
    orr x4, x4, x5
    and x6, x6, x5
    subs x2, x2, #1
    b.ne 1b
WEND wrdloop

    .p2align 14
    .globl SYM(window_programs_finish)
SYM(window_programs_finish):
    nop
