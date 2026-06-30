# skate3 — porte jogável (rexauto)

"Rodar rexauto no skate3 -> jogável" (TU 3.0.3.0, gameplay context reached, 0 FATAL,
zero-regressão nos outros jogos). Snapshot dos arquivos hand-crafted do port
(autoports/skate3/port). generated/ e out/ são regeneráveis (não incluídos).

## A cura que faz jogar
- **src/skate3_exception_compat.cpp** — override do exception-guard da CRT do TU
  (sub_82F6FAA0) forçando r3=0. Sem isso: vtable null -> call 0x0 @lr=0x8291C138 ~16s.
  O guard se MOVE com o TU (retail 0x82F44E40 -> TU 0x82F6FAA0); o rexauto setou
  setjmp_address=0x82F44E40 (mis-ID do exception-guard como setjmp) no manifest.
- **skate3_functions.toml** — inclui os 10 chunks da jump-table de 0x8294AF70
  (0x8294B2C4..0x8294B4A0, parent=0x8294AF70) que faltavam.
- **src/** app completo portado da comunidade (mchughalex/skate3recomp): Skate3BaseApp
  + helpers (fov, user_settings, iso_installer, ultrawide).

## TODO generalizar no pipeline
A auto-detecção de setjmp do rexauto confunde o exception-guard da CRT com setjmp e
nao reajusta o endereco quando um TU e aplicado. Corrigir beneficia todo jogo com TU.
