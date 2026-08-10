# bench/

Benchmark harness and published results (PRD §34). **The only statistics this project publishes.**

Rule E1 (invariant I9): every number in `README.md`, `docs/`, the UI or a release note carries an
inline `[bench:<filename>]` marker naming a committed file in `bench/results/`. `just check-bench`
fails the build on a number without a marker and on a marker without a file. Methodology for every
published number is documented here.
