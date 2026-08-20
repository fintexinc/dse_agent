# `.dse/validation.json` — the repository's contract with the DSE

Everything the DSE knows about *how to check your repository* comes from this
one file. Without it there is no lint, no typecheck, no test and no build gate:
L1 reports `NOT_CONFIGURED`, the preview has no build command to run, and the
only real reviewer left is the human reading the PR.

This is the complete reference. Every field lists what it does, its default,
what happens when it is missing, and — where we have one — the failure we
actually measured because of it.

---

## Where the file is read from

Not one answer, and the difference is the whole governance story:

| Block | Read from | Consequence |
| --- | --- | --- |
| `commands`, `timeout_seconds`, `timeouts`, `sast_severity_gate` | the **base SHA** (`git show <base_sha>:.dse/validation.json`, inside the sandbox) | a PR cannot loosen the gates it is about to face |
| `forbidden_paths` | the **base SHA** (GitHub API, at plan time — there is no sandbox yet) | a PR cannot remove its own path protection |
| `preview` | the **PR branch** | a PR *can* change how its own preview is built, on purpose: the preview is a disposable environment, not a gate |

Changing anything read from the base SHA therefore costs a human-reviewed merge,
and only the *next* work item sees the change. That is what makes it safe to let
the repository own its own protection.

## Hard limits on the file

- **64 KiB** maximum. Larger is an ERROR.
- Must be a JSON **object**.
- **Unknown top-level fields are an ERROR**, and the error takes the whole file
  with it: lint, typecheck, test and build all become `NOT_CONFIGURED` at once.
  A typo in one key silently disables every gate you wrote, so add fields only
  from the list below.

---

## `version` *(required)*

Must be exactly `1`. Anything else is an ERROR.

```json
{ "version": 1 }
```

## `commands` *(required)*

An object whose keys are `lint`, `typecheck`, `test`, `build`. Any other key is
an ERROR.

**A command that is absent or `null` is NOT a disabled gate.** The stage reads
`NOT_CONFIGURED`, and `NOT_CONFIGURED` **fails the pipeline** — a manifest with
no test command is treated as a misconfiguration, never silently blessed. To
turn a gate off on purpose, declare it in `disabled_stages` (below); that is
the honest switch, and it keeps the command intact for the consumers that read
it (the preview build does).

Each command is an **argv array**, never a shell string:

```json
"commands": { "lint": ["ruff", "check", "."] }
```

A string is refused with `commands.<name> must be a JSON array of arguments,
never a shell string`. Limits: at most **128** arguments, each at most **4096**
characters, non-empty, no NUL byte.

**Trap — `build` doubles as the preview's build recipe.** The `deployable`
preview reads `commands.build[2]` verbatim, i.e. the third element of the argv.
A build written as `["mvn", "-B", "package"]` gives the preview `package`, which
is not a command. If your repository has a preview, write `build` in the
`["sh", "-c", "<the whole line>"]` shape — that is why every real manifest here
looks like that.

**Trap — `;` is not `&&`.** A chained recipe like
`["sh", "-c", "npm run build; npx serve"]` throws away the exit status of the
build: the shell reports the status of the *last* command. A repository whose
build was broken for days reported PASS this way. Use `&&`.

## `timeout_seconds` *(optional)*

Seconds allowed for **each** command. Default `300`, accepted range `1..3600`.

The number is **clamped, never refused**: the whole L1 pipeline runs inside a
single Temporal activity, so the *sum* of the stage clocks has to fit that
activity's `start_to_close`. A scalar bigger than the largest share that fits is
lowered, and the L1 evidence says so in plain text.

## `timeouts` *(optional)*

Per-stage seconds, when one number for four commands is not enough. Valid keys:
`lint`, `typecheck`, `test`, `build`, `sast`, `secret_scan` — the last two are
the platform's own scans, but they spend the same activity budget, which is why
you can tune them.

```json
"timeouts": { "lint": 600, "test": 900, "sast": 120 }
```

A per-stage value **wins outright** over the scalar. Declared values that
overflow the activity budget are **shrunk to fit**, not refused.

**Trap — this used to escalate the item.** `bmo-fee-calculator-be-dse` declared
900+900+900+900+120+120 = 3840s against a 3330s budget. The manifest was refused
whole, every command read `NOT_CONFIGURED`, and the item escalated with **zero
stages run** — after the Tester had already passed with a real Java test. Values
are now fitted; the fit is reported in the evidence.

**`test` has a floor.** With no `timeouts.test`, the test stage gets at least
**1500s**, whatever the scalar says. The Tester ran that same suite one activity
earlier on its own 1200s clock; L1 must not cut short a suite the platform
itself already allowed to run. A scalar *above* the floor still governs.

## `sast_severity_gate` *(optional)*

`"LOW"`, `"MEDIUM"` or `"HIGH"` — the severity at which a SAST finding fails the
gate. Default `MEDIUM`. Anything else is an ERROR.

**Trap — this spends the Coder's retries.** Set to `LOW` on a repository with a
noisy scanner, every turn burns an attempt on findings nobody intends to fix,
and the item dies on the retry cap rather than on the work.

## `preview` *(optional)*

What the repository says about its own preview environment. Every field is
optional and absent means "keep the platform default", so a repository that
declares nothing sees no change.

| Field | Type | Meaning |
| --- | --- | --- |
| `image` | string | container image the preview runs in (e.g. `eclipse-temurin:21-jdk`) |
| `artifact_glob` | string | where the build artifact lands (e.g. `bootstrap/target/*.jar`) |
| `env` | object | environment variables for the preview process; values are stringified |
| `port` | int | the port the app listens on inside the container |
| `ready_timeout_s` | int | how long to wait for readiness; **maximum 1050** |
| `start` | argv array | **how the process boots and serves** — e.g. `["sh","-c","java -jar bootstrap/target/*.jar"]`, `["./bin/server"]`, `["npx","vite","preview","--host","0.0.0.0"]` |

**`start` is the field that makes previews work outside the JVM and npm.**
Without it the platform has to guess, and its guess is one of two shapes: find
an artifact and `java -jar` it, or walk an npm dev-server ladder. Both are
wrong for Go, Python, Ruby, .NET, Rust and PHP — and wrong for a frontend that
is not Angular. Declare it and the platform stops guessing.

If your repository already has a manifest without it, you do not have to write
the amendment yourself: the DSE opens a PR proposing it the next time it works
on the repository, and the task it was running carries on meanwhile.

## `install` — the dependency step (top level)

```json
{ "install": ["npm", "install", "--no-audit", "--no-fund"] }
```

One repository installs its dependencies one way, so this key sits at the top
of the manifest and has **two consumers**: the sandbox where the DSE writes and
runs tests, and the preview Pod. It is memoized per Pod — declaring it costs one
install per work item, not one per step.

It is not inside `preview`. Preparation that only the preview needs belongs in
`preview.build`, which already runs there.

## `commands.test_subset` — the suite, restricted to given files

```json
{ "commands": { "test": ["npx", "jest", "--ci"],
                "test_subset": ["npx", "jest", "--ci", "--coverage=false"] } }
```

After the DSE writes a test it asks a small question: *do the tests I just
wrote execute and pass?* — not *is this whole repository green*, which is the
L1 gate's question minutes later. `test_subset` answers the small one: the
platform appends the paths of the files it just authored.

Two things belong to you and not to the platform:

- **Whatever flag a partial run needs.** A jest with `collectCoverage: true`
  and global thresholds fails *any* subset on coverage while every test passes
  — measured at 9.83% against a floor of 80%. `--coverage=false` is a fact
  about your jest config, so it lives here, in your manifest.
- **Whether to declare it at all.** Runners that take filters rather than paths
  (maven, dotnet, cargo) should omit it. The DSE then runs the whole
  `commands.test` — slower, but never a command it invented.

`test_subset` is a command, not a gate: it has no `timeouts.test_subset`, it
cannot be listed in `disabled_stages`, and it never produces a verdict of its
own.

Unknown fields inside the block are an ERROR — a typo (`imagen:`) must be an
explained failure, not a silent default.

**Trap — `ready_timeout_s` above 1050 buys nothing.** The trigger activity dies
at 1200s and the margin pays for log capture, the DB write and the PR comment.
Asking for more trades "degraded at 15 minutes" for "attempt lost".

**Trap — a preview needs HTTPS to be useful.** Anything behind Auth0 (or any
provider that requires a secure origin) renders a blank page over `http://`.

## `disabled_stages` *(optional)*

Repository gates this repository has turned off, by name:

```json
"disabled_stages": ["test", "build"]
```

- Valid entries: `lint`, `typecheck`, `test`, `build` — **only the
  repository's own gates**. `sast` and `secret_scan` belong to the platform (a
  repository cannot disable the secret scan) and are refused, as is any
  unknown name.
- A disabled stage runs **nothing** and reports
  `not run: disabled by the repository manifest (disabled_stages)` — a PASS
  that says exactly why it passed. The ledger never shows a silent green.
- The `commands` entries stay untouched, so consumers that read them keep
  working — the preview's build recipe reads `commands.build[2]` from this
  same file.
- Read from the **base SHA**, like `forbidden_paths`: a PR cannot disable the
  gates it is about to face; turning one off costs a reviewed merge.

**Why this field exists** (measured, 2026-08-19): the two obvious routes are
both wrong. Nulling a command makes the stage `NOT_CONFIGURED`, which fails the
whole pipeline with an impossible fix ("add a test command" — the manifest is
read from the base SHA, so the Coder cannot). And an `echo` stub cannot pass
the test gate's evidence rule without printing a fake test count — a ledger
that lies. This field is the honest door.

**Trap — this is a pause button, not a policy.** A repository with `test`
disabled ships changes nobody's suite looked at; the PR reviewer is the only
gate left. Re-enable as soon as whatever forced the pause is fixed.

## `forbidden_paths` *(optional)*

Paths in this repository that a plan must not write to without a human saying so.

```json
"forbidden_paths": [".github/workflows/", "migrations/", "config/production/"]
```

- **Absent** → the platform default, `[".github/workflows/", "migrations/"]`.
- **`[]`** → this repository protects nothing. A deliberate decision, and it
  costs a reviewed merge on the base branch.
- Syntax: path **segments**, matched at **any depth** — `migrations/` covers
  `services/api/migrations/0001.sql` but not `migrations_backup/`. A leading `/`
  pins the pattern to the repository root (`.gitignore`'s own convention). No
  globs, no wildcards.
- Refused: an entry that normalises to empty (`""`, `"/"`) — it would mean the
  whole repository — and any entry containing `..`. At most 64 entries, each at
  most 256 characters.

**How the protection behaves.** When the plan's `expected_files` intersects
`forbidden_paths`, the work item **stops at the human gate regardless of risk
class**, and the gate message names the files. Approving authorises **exactly
those files**: L1 lets them through and still fails anything else that shows up
under a protected path.

**Trap — this list used to make ordinary tasks impossible.** It was a platform
constant applied to every repository. "Add a GitHub Actions workflow" produced a
plan declaring `.github/workflows/ci.yml` *and* forbidding
`.github/workflows/` — the only diff that passed the gate was the diff that did
not do the work. Roughly US$ 4 and 40 minutes burned before the item died on
`coder_not_converging`. Protect what actually matters in *your* repository, and
nothing more.

---

## A complete, real example

`fintexinc/calculation-engine-service` — a multi-module Java 21 / Maven service:

```json
{
  "version": 1,
  "timeout_seconds": 1800,
  "timeouts": {
    "lint": 600, "typecheck": 600, "test": 900,
    "build": 900, "sast": 120, "secret_scan": 120
  },
  "commands": {
    "lint": ["sh", "-c", "./mvnw -B -q spotless:check"],
    "typecheck": ["sh", "-c", "./mvnw -B -q -DskipTests compile"],
    "test": ["sh", "-c", "./mvnw -B test"],
    "build": ["sh", "-c", "./mvnw -B -q -DskipTests package"]
  },
  "sast_severity_gate": "MEDIUM",
  "preview": {
    "image": "eclipse-temurin:21-jdk",
    "artifact_glob": "bootstrap/target/*.jar",
    "port": 8181,
    "ready_timeout_s": 1050,
    "env": {
      "SPRING_PROFILES_ACTIVE": "dev",
      "SM_REST_BASE_URL": "http://security-master.invalid",
      "AZURE_MONITOR_ENABLED": "false"
    }
  }
}
```

Two things this example does on purpose, both learned the hard way:

- `artifact_glob` is `bootstrap/target/*.jar`, not `target/*.jar`. The default
  assumes a single-module build and would never find the artifact here.
- `SM_REST_BASE_URL` points at an unroutable host. The app fails fast at startup
  without it; the preview only needs the process to stay up, not to reach the
  real dependency.

The live file also wraps each command in a `JAVA_HOME` discovery prefix, because
the sandbox image ships more than one JDK. That belongs to that repository's
environment, not to the manifest format — which is exactly the point of the
`["sh", "-c", ...]` shape.

## Don't have this file yet?

You may not need to write it by hand: send any task to the DSE on a repository
without the manifest and it opens a **bootstrap PR** with a proposed contract —
commands mirrored from the repository's own CI files, validated by the same
parser that will enforce them. Review it, adjust it, merge it, then resend your
task. (An open bootstrap PR is reused, never duplicated.)

## Checklist before you commit it

1. It is valid JSON and under 64 KiB.
2. `version` is `1`, and there is no key outside the list above.
3. Every command is an array, and `build` is `["sh", "-c", ...]` if this
   repository has a preview.
4. Chained shell commands use `&&`, never `;`.
5. `forbidden_paths` protects what matters *here* — an inherited list protects
   badly twice: it covers what does not matter and misses what does.
