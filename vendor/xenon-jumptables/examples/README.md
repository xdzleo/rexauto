# Example

A minimal walkthrough. No game binaries are included or required to read this —
bring your own dump.

## 1. Gather inputs

From your recompiler:

- **Raw code image** — the dumped guest image (here `game.bin`).
- **Address ranges** — printed at startup. For ReXGlue / XenonRecomp:

  ```
  Function table initialized for module: code=82080000-8230EC00, image=82000000-826D0000
  ```

  → `text_start=0x82080000`, `text_end=0x8230EC00`,
    `image_base=0x82000000`, `image_end=0x826D0000`.

- **Function list** — from the generated per-function sources:

  ```sh
  python ../src/extract_funcs.py /path/to/generated -o functions.txt
  ```

## 2. Configure

Copy `config.example.json` and fill in the paths/ranges for your title
(addresses above are an example).

## 3. Run

```sh
python ../src/recover.py config.json --ida "/path/to/idat"
```

Produces `switch_tables.toml`. On a full retail image the IDA pass takes a few
minutes (it defines every function and iterates to a coverage fixpoint).

## 4. Use

Add `switch_tables.toml` to your recompiler config / manifest, apply
[`../patches/switch-on-ctr.patch`](../patches), regenerate, and rebuild. See
[../docs/integration.md](../docs/integration.md).

## What you should see

```text
[xjt] functions=11384 (add_func=2172)
[xjt] raw bctr opcodes=356
[xjt] round 0: new=74 total=74
[xjt] round 1: new=0 total=74
[xjt] DONE tables=74  ->  .../jumptables.json
wrote .../switch_tables.toml (xenonrecomp): 74 tables, 2955 targets
```

(Paths are abbreviated above; the tools print full paths.) Of the 356 `bctr`s in
that image, most are virtual / function-pointer dispatches (correctly left
alone); 74 are static jump tables, now resolved.
