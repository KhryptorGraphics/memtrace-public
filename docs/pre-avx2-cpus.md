# Running memtrace on pre-AVX2 CPUs

The ONNX Runtime build memtrace bundles by default is compiled with an AVX2
baseline. On a CPU that lacks AVX2 (Intel Ivy Bridge / Xeon E5 v2 and older,
AMD pre-Excavator), the embedding and rerank paths terminate the process
with `trap invalid opcode` partway through model load.

memtrace v0.3.56+ detects this at startup and refuses to launch with a clean
error rather than crashing inside the runtime. v0.3.66+ also ships
dedicated `*-noavx2` platform packages that npm picks automatically at
install time on pre-AVX2 hosts.

## TL;DR

If you're on memtrace ≥ v0.3.66, the auto-install path should just work:

```sh
npm install -g memtrace@latest
memtrace start
```

`npm install` runs a CPU detection at install time (reads `/proc/cpuinfo`
on Linux, `IsProcessorFeaturePresent` on Windows, `sysctl
machdep.cpu.leaf7_features` on macOS) and pulls the matching `*-noavx2`
platform package on a pre-AVX2 host. No env vars, no manual builds.

If you upgraded from an older memtrace and the gate still fires, force
a clean reinstall:

```sh
npm uninstall -g memtrace
npm install -g memtrace@latest
```

Override the auto-detection if it gets it wrong:

- `MEMTRACE_FORCE_NOAVX2=1 npm install -g memtrace` — force pre-AVX2 install
- `MEMTRACE_FORCE_AVX2=1 npm install -g memtrace` — force AVX2 install

If the auto-install doesn't work for your environment (locked-down
container, custom binary distribution, etc.), the manual workarounds
below still apply.

## Symptom

Process exits at startup with:

```
memtrace: this CPU does not support AVX2, which the bundled ONNX Runtime requires.
```

Or, on older builds, in `dmesg`:

```
traps: tokio-rt-worker[…] trap invalid opcode ip:… in memtrace[…]
```

Confirm the CPU lacks AVX2:

```sh
grep -o 'avx2' /proc/cpuinfo | head -1   # prints nothing on pre-Haswell
lscpu | grep -i avx                       # no `avx2` in the flags list
```

## Workaround: supply a non-AVX2 ONNX Runtime build

memtrace links ONNX Runtime dynamically. You can point it at any compatible
`libonnxruntime` shared library via `MEMTRACE_ORT_DYLIB_PATH`. Versions
pinned to ONNX Runtime 1.20.x are known to work with this release.

### Option A — Microsoft's official prebuilt (no AVX2 variant)

Microsoft does not currently ship a pre-AVX2 prebuilt for v1.20.x on any
platform. If you find one published by your distro or a trusted third
party, drop it into a directory and:

```sh
export MEMTRACE_ORT_DYLIB_PATH=/opt/onnxruntime-noavx2/lib/libonnxruntime.so
memtrace start
```

### Option B — build ONNX Runtime from source

This is the supported path for pre-AVX2 hardware. Build flags below disable
every AVX/AVX2/AVX-512 codegen path:

```sh
git clone --recursive --branch v1.20.0 \
  https://github.com/microsoft/onnxruntime.git
cd onnxruntime
./build.sh \
  --config Release \
  --build_shared_lib \
  --parallel \
  --skip_tests \
  --cmake_extra_defines \
      onnxruntime_DISABLE_AVX=ON \
      onnxruntime_DISABLE_AVX2=ON \
      onnxruntime_DISABLE_AVX512=ON
```

The resulting `libonnxruntime.so.1.20.0` lives at
`build/Linux/Release/libonnxruntime.so.1.20.0`. Symlink or rename it to
`libonnxruntime.so`, then:

```sh
export MEMTRACE_ORT_DYLIB_PATH=/path/to/build/Linux/Release/libonnxruntime.so
memtrace start
```

### Option C — replace the bundled dylib in place

If you'd rather not set an env var, drop your non-AVX2
`libonnxruntime.{so,dylib,dll}` directly next to the `memtrace` binary:

```sh
which memtrace                              # resolves through npm's bin dir
ls -l "$(dirname "$(readlink -f "$(which memtrace)")")"
```

memtrace's auto-discovery picks up a `libonnxruntime.*` sitting next to the
binary at startup. This survives `memtrace stop` / `memtrace start` cycles
but **not** `npm update -g memtrace` — the next package install overwrites
it. Prefer option B for long-lived hosts.

## Resolution order

memtrace resolves the ONNX Runtime dylib in this order. The first match
wins:

1. `ORT_DYLIB_PATH` — the lower-level escape hatch read directly by the
   `ort` crate. Set this if you need to override everything else.
2. `MEMTRACE_ORT_DYLIB_PATH` — the supported public override. Use this for
   pre-AVX2 hosts or any custom build.
3. `libonnxruntime.{so,dylib,dll}` next to the `memtrace` binary — the
   bundled default that ships with the per-platform npm package.

If none are found, the OS loader's default search path (`LD_LIBRARY_PATH`,
`DYLD_FALLBACK_LIBRARY_PATH`, `PATH` on Windows) takes over. memtrace will
exit with a load error if no matching dylib is found there either.

## When the gate refuses to launch

If you've supplied a non-AVX2 dylib via `MEMTRACE_ORT_DYLIB_PATH` and the
gate still exits with the AVX2 error, double-check:

- `MEMTRACE_ORT_DYLIB_PATH` is exported in the same shell that runs
  `memtrace start`. The gate reads it from the parent process's
  environment — `.env` files loaded after launch don't count.
- The path is absolute and the file exists. Relative paths resolve against
  the binary's working directory at start.

If both look correct and you still hit the gate, file an issue with the
output of `memtrace --version`, `lscpu | head -20`, and your dylib's
provenance.
