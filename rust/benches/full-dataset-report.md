# Full-dataset query benchmark

This is an observational report for the current USA extract in
`data/tanks_data.json`. It is **not** a hardware-independent latency
guarantee and is not used as a CI threshold. The Milestone 4
microsecond gate applies only to
`rust/tests/fixtures/benchmark_tanks_data.json` in release mode with
`--max-p95-us 1000`.

Load and graph-construction time are excluded from the samples below.

## Environment

| Item | Value |
| --- | --- |
| Application | tank-graph 0.1.0 |
| Build profile | release |
| rustc | 1.96.1 (31fca3adb 2026-06-26) |
| OS | windows |
| Arch | x86_64 |
| Dataset | `data/tanks_data.json` (USA extract) |
| Graph nodes | 5289 |
| Graph edges | 171102 |
| Warm-up | 1000 |
| Measured iterations | 10000 |

## Class query: `Heavy Tanks`

| Metric | Value |
| --- | --- |
| Result count | 27 |
| min | 129.3 µs |
| median | 204.8 µs |
| p95 | 262.3 µs |
| max | 608.1 µs |

Command:

```powershell
cargo run --release -- --data ..\data\tanks_data.json --format json benchmark class --name "Heavy Tanks" --warmup 1000 --iterations 10000
```

## Keyword query: `sidescraping`

Keyword traversal follows every shared strategy-to-keyword edge, so sample
time grows with graph density. On this extract the p95 is milliseconds, not
sub-millisecond. That does not fail the fixture gate.

| Metric | Value |
| --- | --- |
| Result count | 109 |
| min | 9805.6 µs |
| median | 10899.85 µs |
| p95 | 14233.7 µs |
| max | 26361.2 µs |

Command:

```powershell
cargo run --release -- --data ..\data\tanks_data.json --format json benchmark keyword --name "sidescraping" --warmup 1000 --iterations 10000
```
