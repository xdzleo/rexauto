# skate3 — porte jogável (rexauto)

"Rodar rexauto no skate3 -> jogável" (TU 3.0.3.0, "gameplay context reached", 0 FATAL,
zero-regressão nos outros jogos). Snapshot dos arquivos hand-crafted; generated/ + out/
regeneram (não incluídos).

## A cura — agora GENERALIZADA no pipeline (melhor que o hand-code deles)
- Exception-guard do TU: stage_setjmp detecta o guard na imagem PATCHEADA e grava
  setjmp_address=0x82F6FAA0 no manifest -> codegen emite ppc_setjmp nos call-sites
  (retorna 0 na chamada direta = "sem exceção", r3=0). NÃO precisa mais de C++ por
  jogo. src/skate3_exception_compat.cpp fica como referência mas NÃO é linkado.
- skate3_functions.toml: inclui os 10 chunks da jump-table de 0x8294AF70.
- src/: app completo portado da comunidade (mchughalex/skate3recomp).

## Fix de pipeline (beneficia a frota com TU)
Causa do mis-setjmp = dump de imagem stale (escaneava a base, não a TU). stage_setjmp
agora força dump fresco -> escaneia a imagem que o codegen recompila. No-op pra jogos
sem TU (codegen byte-idêntico).
