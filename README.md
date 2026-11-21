# Blast Radius starter

A submission for the Blast Radius track. It maps patched functions and fixtures to tests that exercised them on the pinned SQLGlot checkout. Unrecognized changes and possible import failures fall back to the full suite.

## The problem

Your image gets a batch of code changes to [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. For each change, print which tests to run and how to split them across up to 8 workers.

| | |
| --- | --- |
| `/data/commits/<id>.patch` | One unified diff against the pinned commit. Each stands alone: none builds on another. |
| `/data/tests.json` | Every test, numbered, with its file and the seconds it took us to run. |
| stdout | One JSON line a commit: `{"commit": "k7q2", "workers": [[12, 845, 901], [3, 77]]}` |

Each inner list is one worker's test numbers. `"workers": []` runs nothing, which is the right answer for a change that breaks nothing.

## Scoring

Lower is better. [SCORING.md](SCORING.md) has the exact rules; in short:

- A commit costs the recorded seconds of its busiest worker, plus 2 seconds for every worker.
- A commit where you left out a test that fails adds 300 seconds.
- The CPU seconds your program uses are added. Running sqlglot's tests while you decide is allowed, and you pay for the time.
- Your image runs 3 times and the middle total counts.

"Fails" means pytest reports a failure, an error or a timeout for the test, or the test's file cannot be imported. A test with subtests is one test: one failing subtest fails it.

## Try it

You need Docker, make and Python 3.9 or newer. The kit unpacks into the folder you are in, so unpack it into an empty one.

```sh
make sample   # build, run once on the 30 sample commits under the scorer's limits, and score
make check    # the output's format only
```

`make sample` prints your simulated seconds and misses, which are exact for that output. The board adds the CPU seconds your container used and takes the middle of 3 runs, so expect it a little higher. Your program never sees `sample/answers.json`: only the patches and `tests.json` are mounted.

`sample/answers.json` lists the tests each sample commit breaks and the kind of change it is. The practice and final commits are hidden, and so is everything your program prints when it runs on them. A rejected submission tells you which rule it broke and nothing more, so reproduce problems here.

`coverage.json.gz` contains per-test function and file dependencies captured on the pinned checkout. It also records expression validation, dialect week-offset reads, and numeric escape lookups by dictionary owner and key. The selector uses those reads for literal value changes that preserve the field's structure. It uses the full suite when an affected read runs during collection.

When a flag makes an expression argument required, the selector includes tests that validated the expression with that argument missing. It also retains validations with custom behavior or multiple missing required arguments, since the change can reorder their errors.

For an identity-to-equality edit at a profiled comparison, the selector includes tests with a different observed result, an unverified equality method, or direct access to the modified source or code. The baseline probe evaluates only verified pure comparisons.

For an isolated `and`/`or` or `==`/`!=` change in a profiled guard, the selector includes tests that reach the condition. It uses the same approach for same-type literal edits in local assignments, return values, and direct arguments of returned calls. Both rules include tests that inspect the source or enclosing code. If an edited function contains a lazy import, the selector schedules the full suite because the import can affect later tests.

For a strict-to-inclusive comparison edit or its reverse, the selector checks where the operands were equal. It uses that evidence for comparisons against literal integers or integer tuples with verified operand types. It keeps function-level coverage when a test uses an unverified operand.

For an isolated SQL fixture edit, the selector checks whether the standard loaders would yield the same input, expected output, and ordered metadata. Tests that read the raw file still run.

`import_map.json.gz` records which tests use modules and which parser factories only create closures. The selector uses that map for same-type literal edits to those factory calls. For observed `SHOW_PARSERS` entries it can use exact calls recorded in the coverage map, with module-level selection when that trace is incomplete.

Run `make imports` and then `make coverage` to regenerate the maps from the checked-in baseline data. Ordinary builds reuse both maps. Run `python3 -m unittest test_impact` to check patch classification.

The Docker build saves syntax summaries for the pinned Python files. The selector loads each summary on demand and checks it against the source file before using it.

## Submit

Push to a public GitHub repository with the `Dockerfile` at its top, then enter the repository and the commit hash on the track page. The final scores the commit you pick. If you pick none, your best practice submission counts.

## Rules

- Submit your own team's repository. A submission with the same repository, commit or archive as another team's is disqualified, the later one.
- Your build may download only from public package registries: PyPI, npm, Docker Hub, GitHub releases at a fixed tag, and sqlglot’s own repository at the pinned commit. Nothing from an address your team controls. The final rebuilds every image after the cutoff, so anything else could change a submission after the deadline.
- No answers worked out ahead of time and looked up by patch: no table from a patch, or from a kind of edit, to the tests it breaks. Maps built from the pinned commit (coverage, imports, fixtures) are allowed. Top entries are read.
- Only the final counts. The practice board carries no prize.

## How your image runs

- The build has the network, 4 cores, 8 GB of memory and 10 minutes. It may clone sqlglot at the pinned commit, install it and run its tests to make coverage or import maps.
- Your repository may hold 20 MB. Git submodules and Git LFS files are not fetched: clone sqlglot in the `Dockerfile` and commit maps as plain files.
- The run has no network, 2 cores, 4 GB of memory and 3 minutes for the whole batch (4.5 in the final, which has about one and a half times as many commits).
- The disk is read-only. `/tmp` holds 64 MB and is empty at the start of every run.
- The container runs as user `nobody`, with `HOME=/tmp` and at most 256 processes.
- stdout may hold 2 MB. More rejects the run.

## Things that will catch you

- Logs go to stderr. Any line on stdout that is not an answer rejects the run.
- `score.py` names a broken rule by its code, as in `Rejected: not_json on line 1`. The table in SCORING.md says what each code means.
- Files your program needs must be readable by `nobody`. A map written by `root` during the build with mode 600 is not.
- pytest 9 prints a test as `PASSED` beside `SUBFAILED` lines when one of its subtests fails. That test failed.
- sqlglot skips its executor tests unless `SKIP_INTEGRATION=0` is set. We labeled with it set, so those tests can fail.
- `requirements.lock` is every package in the environment we labeled in, on the image the `Dockerfile` starts from. To run sqlglot's tests the way we did, `pip install -r requirements.lock` and put the sqlglot checkout on `PYTHONPATH`; sqlglot's own `pip install -e .[dev]` picks newer versions.
- The scorer is linux/amd64. On an Apple Silicon Mac, build and run with `--platform linux/amd64`, as the Makefile does, or your timings and wheels will differ.
- A change that only adds a comment is not promised to be harmless.
