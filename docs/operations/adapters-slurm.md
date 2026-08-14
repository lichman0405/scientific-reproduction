# Slurm-over-SSH ComputeAdapter: configuration, operations, limitations

This guide documents the v0.1 Slurm-over-SSH compute backend (AC-03 of
DEV-M13-G04): how `SlurmComputeAdapter` is configured, what its operations
do, and — precisely — what it cannot do. It is grounded in the
implementation `src/scientific_reproduction/adapters/compute/slurm_ssh.py`
and `adapters/compute/ssh.py` (the transport boundary), the sibling backends
`adapters/compute/local.py` and the frozen specs `15-ADAPTER-SPEC.md`
(SS2/SS3/SS5/SS6), `11-COMPUTATION-SUBSYSTEM.md` (SS4–SS6) and
`01-PRODUCT-REQUIREMENTS.md` (SS9).

## 1. What the adapter is

`SlurmComputeAdapter` (`adapters/compute/slurm_ssh.py`, DEV-M7-G03) is the
scheduler-backed compute backend: it submits batch scripts through Slurm's
`sbatch`, inspects them through `squeue` (active states) and `sacct`
(terminal states), and cancels them through `scancel`. It stamps
`backend = "slurm_ssh"` and `ADAPTER_ID = "compute/slurm_ssh"` (record
version `1.0`) into every durable job record; the plain remote-shell variant
is `backend = "ssh"` (`ADAPTER_ID "compute/ssh"`) and the v0.1 reference
backend is `local` (`ADAPTER_ID "compute/local"`).

## 2. Configuration

Configuration is **constructor-bound**: the v0.1 codebase ships no
configuration-file loader for compute adapters, so every knob is a
constructor argument (the real code path for the project/user configuration
layer of `15-ADAPTER-SPEC.md` SS6 — nothing reads env vars or config files
at runtime). The constructor signature (verified in
`slurm_ssh.py::SlurmComputeAdapter.__init__`):

```python
SlurmComputeAdapter(
    credentials: SSHCredentials,
    state_dir: str | Path,
    *,
    transport: SSHTransport,            # required, no default
    modules: tuple[str, ...] = (),
    environment: Mapping[str, str] | None = None,
    retry_policy: SSHRetryPolicy | None = None,   # default SSHRetryPolicy()
    now: Clock | None = None,           # default utc_now
)
```

| Argument | Meaning |
|---|---|
| `credentials` | `SSHCredentials` — see below. Kept in memory only; never written to the state directory. |
| `state_dir` | Where the adapter persists its durable job records, scripts, staging and artifact registry (see section 5). |
| `transport` | The **injectable** remote boundary (`SSHTransport` ABC, `ssh.py`): `connect` / `disconnect` / `is_connected` / `run_command(RemoteCommand) -> RemoteResult` / `push_file` / `pull_file`. Required and keyword-only. |
| `modules` | `module load` statements embedded in the generated batch script (names validated for shell safety). |
| `environment` | Caller-supplied environment snapshot, embedded as `export` lines in the batch script (names/values validated; stored key-sorted). |
| `retry_policy` | `SSHRetryPolicy`: `max_attempts = 3` default, `backoff = default_backoff` (exponential, capped at 30 s: `min(30.0, 0.5 * 2**(attempt-1))`). Bounds reconnect attempts of pending transport operations. |
| `now` | Injected clock for deterministic timestamps. |

`SSHCredentials` (`ssh.py`, frozen dataclass):

| Field | Default | Constraint |
|---|---|---|
| `host` | — | required, non-empty, no whitespace |
| `port` | `22` | int in `1..65535` |
| `username` | `None` | remote login name or None |
| `password` | `None` | password, or None when key-based login is used |
| `private_key_path` | `None` | path of the private key file |
| `key_passphrase` | `None` | passphrase of the private key |

## 3. Operations

All operations re-hydrate the durable record from disk (the M1 recovery
discipline — a fresh adapter instance over the same `state_dir` recovers the
job from its record alone), run each remote step under the retry policy in
its own connect/disconnect session, and are rejected when the job is not in
the required state.

- `prepare(run_context)` — validates the context (remote working directory
  and every declared output name must be safe path segments), derives the
  deterministic `job_id` (`generate_id("job", run_context.run_id)`), writes
  the durable job record in `prepared` state, stages the run.
- `submit(run_context)` — generates the batch script
  (``<state_dir>/scripts/<job_id>.slurm.sh``, stamped by the adapter), makes
  the remote working directory (`mkdir -p --`), pushes the script, and runs
  `sbatch --chdir <workdir> --output <workdir>/.sr_<job_id>_job.log -- <script>`.
  Parses the `Submitted batch job <id>` answer into the **external Slurm job
  id** (AC-01); a clean remote refusal or an unparseable answer raises
  `SlurmJobLaunchError` (job-level, never retried); a permanent transport
  failure records `failure_class="transport"` and re-raises. A second submit
  of the same job is rejected.
- `status(job_id)` — probes the scheduler **by the recorded external id
  only**: `squeue --jobs <external_id>` for active states, `sacct --jobs
  <external_id>` for terminal states, normalized through
  `normalize_scheduler_state` (section 4).
- `collect(job_id)` — pulls the declared outputs and computes the artifact
  checksums into the artifact registry when the job completed successfully;
  a failed job's collect is refused with the recorded failure.
- `cancel(job_id)` — `scancel` on the recorded external id.
- `resume(job_id)` — used by the engineering-retry path (see
  `docs/user/monitor-and-handoff.md` section 3.4); enforces a restartable
  state.
- `read_job(job_id)` — the durable record as a typed `SlurmJobRecord`.

## 4. State normalization

The `SLURM_STATE_RULES` table (stable rule ids `R-SLURM-S1..S27`,
`slurm_ssh.py`) maps the scheduler's compact state vocabulary — `squeue`
`%T` values (`PD` pending, `CF` configuring, `S` suspended, `R` running,
`CG` completing, `RQ` requeued) and `sacct` `State` values (`CD` completed,
`CA` cancelled, `F` failed, `TO` timeout, `NF` node failure, `OOM` out of
memory, `PR` preempted) — onto the project Run lifecycle. `R-SLURM-S27` is
the **trailing total default**: any observed state with no rule is decided
`failed`. Terminal states are persisted once and never re-opened
(`SlurmJobRecord.from_dict` rejects records whose backend/version/state
violate the contract). When neither `squeue` nor `sacct` reports the job,
the record carries `SLURM_STATE_UNAVAILABLE_NOTE`: "neither squeue nor
sacct reported it; job recorded as completed" — a visibility-fallback
completion, not a proof of success. `status` hands the normalized decision
to the Execution Monitor, which completes a Run only on an exact
`RESULT_AVAILABLE` signal (`monitoring/reconcile.py`; see
`docs/user/monitor-and-handoff.md`).

## 5. Durable state layout

Under the injected `state_dir`:

```text
<state_dir>/
  jobs/<job_id>.json      durable SlurmJobRecord (backend, state, external id,
                          command, working dir, outputs, modules, environment,
                          failure_class, timestamps) — canonical JSON, no
                          credential fields (AC-02)
  scripts/<job_id>.slurm.sh   generated batch script
  staging/                run staging area
  artifacts/              ArtifactRegistry of collected outputs
```

Job records are written atomically; `from_dict` validates the record on
read, so a corrupt or foreign record fails loudly instead of being trusted.

## 6. Failure classification

Every durable record carries `failure_class` — `"transport"` (connection
level: unreachable host, authentication failure, dropped connection) or
`"job"` (the remote job itself failed) or `None`. This is the boundary the
Execution Monitor's retry whitelist relies on
(`monitoring/retry.py`, `ENGINEERING_RETRY_WHITELIST = {"transport"}`): only
transport failures are identical-resubmitted; a `"job"` class is observed
and never resubmitted (safe by construction).

## 7. Limitations (v0.1, documented and tested)

- **No shipped SSH transport.** The module never imports paramiko; the
  `SSHTransport` ABC is a pure, injectable abstraction and the shipped
  package deliberately ships no concrete implementation. Production use
  requires injecting a transport built on a real SSH client.
- **Exactly-once launch is not guaranteed across a drop.** A mid-operation
  drop reconnects and re-runs the pending step (the allowed engineering
  retry of `11-COMPUTATION-SUBSYSTEM.md` SS5); because the submission may
  have reached the scheduler before the drop, the same job can be launched
  more than once in the pathological window (documented v0.1 limitation,
  resolved in the scheduler-backed DEV-M7-G04 flow).
- **No queue-wide visibility.** Every scheduler query derives its `--jobs`
  argument from the recorded external id; the adapter cannot enumerate the
  queue, so a job whose record is lost is invisible to it.
- **No partition/account/GPU/reservation flags.** `sbatch` is invoked with
  `--chdir` and `--output` only; queue selection is whatever the cluster's
  defaults are.
- **No runtime configuration file or env vars.** All configuration is
  constructor-bound; there is no config-file loader and nothing reads
  environment variables at runtime.
- **Credentials never persist.** `SSHCredentials` lives in memory on the
  adapter only; durable records carry no credential fields.
- **Terminal state depends on accounting.** `sacct` must report the job;
  when neither `squeue` nor `sacct` reports it, the record falls back to
  `SLURM_STATE_UNAVAILABLE_NOTE` (completed without a reported terminal
  state).
- **Per-operation sessions.** Each remote operation opens and closes its own
  connection; there is no long-lived session pooling.

## 8. Grounding

- Code: `src/scientific_reproduction/adapters/compute/slurm_ssh.py` and
  `adapters/compute/ssh.py` (transport boundary, retry policy, credentials),
  `adapters/compute/local.py` (reference backend),
  `src/scientific_reproduction/monitoring/retry.py` (retry whitelist).
- Specs: `15-ADAPTER-SPEC.md` SS2 (role/adapters), SS3 (compute backend
  lifecycle), SS5 (capabilities, `resume_session`), SS6 (configuration);
  `11-COMPUTATION-SUBSYSTEM.md` SS4–SS6 (remote execution, exactly-once
  discussion, durable tasks); `01-PRODUCT-REQUIREMENTS.md` SS9.
