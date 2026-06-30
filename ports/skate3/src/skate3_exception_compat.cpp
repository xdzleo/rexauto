// skate3_exception_compat.cpp -- rexauto port of the community exception-guard fix.
//
// The guest CRT dispatches structured-exception guards through a trampoline
// pointer that real hardware installs during CRT startup. The recompiled runtime
// never installs it, so the recompiled guard returns with r3 untouched (a
// non-zero stack buffer), which its ~17 call sites read as "an exception
// occurred" and silently skip their guarded work -- including the object
// construction whose vtable then stays null (the 0x0 call @lr=0x8291C138 crash
// ~16s into load). Force the setjmp-style "direct return" result (r3 = 0) when
// the trampoline is absent, exactly like the community Skate3 build.
//
// This rexauto build applies Title Update 3.0.3.0, so the guard is sub_82F6FAA0
// and the trampoline is 0x830EF8C0 (retail 3.0.0.0 was guard 0x82F44E40 /
// trampoline 0x83092CC0). The generated guard body is reachable as the weak
// DEFINE_REX_FUNC alias's __imp__ form, so this strong sub_82F6FAA0 overrides it.
#include "generated/default/skate3_init.h"

#include <cstdint>

extern "C" REX_FUNC(__imp__sub_82F6FAA0);

extern "C" REX_FUNC(sub_82F6FAA0) {
  const uint32_t trampoline = REX_LOAD_U32(0x830EF8C0u);
  if (trampoline == 0u) {
    ctx.r3.u64 = 0;
    return;
  }
  __imp__sub_82F6FAA0(ctx, base);
}
