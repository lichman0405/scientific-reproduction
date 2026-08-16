"""Parameterized language packs of the operator-/human-facing renderers (issue #122).

Every operator-facing renderer of the reporting subsystem (the
experiment and computation execution sheets of ``reporting.sheets`` and
the final reproduction report PDF of ``reporting.pdf_report``) renders
its **template strings** -- section titles, labels, markers, footer
text -- through an injected :class:`TemplatePack`. The renderers take an
explicit ``language`` parameter (default ``"en"``) and resolve it with
:func:`resolve_pack`; there is **no runtime locale auto-detection**, so
``(state, language)`` still maps to byte-identical output
(``14-STATE-GIT-ARTIFACTS.md`` SS7: identical state renders identical
bytes -- the language is just another explicit input).

What is in a pack, and what is not
----------------------------------
A pack covers the renderers' own presentation text. The **content**
rendered on the sheets and in the report (manifest procedures, reagent
names, requirement statements, recorded rationales, frozen vocabulary
values such as ``STRICT_REPRODUCTION`` or ``PASS``/``FAIL``) is
registered state or frozen schema vocabulary -- it is *data*, never
translated, and the fidelity rule of the execution sheets (manifest
content matches the sheet 1:1) is language-independent. Schema field
names rendered as labels (procedure step keys, table columns) are also
data and stay verbatim.

PDF output note
---------------
The deterministic PDF writer (``rendering.pdf``) uses base-14 Type1
fonts with WinAnsi (cp1252) encoding; characters cp1252 cannot represent
-- including CJK -- render as ``?`` deterministically. The ``zh`` pack
therefore targets the HTML sheets (the operator-facing artifact, fully
readable in any browser); the PDF renderers accept ``language="zh"`` and
render deterministically, with the writer's documented ``?`` fallback.
A future writer with embedded CJK fonts can consume the same packs
unchanged.

Available languages are explicit: :data:`AVAILABLE_LANGUAGES` and the
:data:`EN_PACK` / :data:`ZH_PACK` instances. Unknown languages raise
``ValueError`` with a stable message; wrong types raise ``TypeError``
(the house boundary convention). Pack fields are plain immutable
strings/format templates -- every renderer is a pure function of
``(state, generated_at, language)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "AVAILABLE_LANGUAGES",
    "ComputationPack",
    "EN_PACK",
    "ReportPack",
    "SheetPack",
    "TemplatePack",
    "ZH_PACK",
    "resolve_pack",
]

#: The languages with a shipped pack, in canonical order.
AVAILABLE_LANGUAGES: Final[tuple[str, ...]] = ("en", "zh")


# ---------------------------------------------------------------------------
# Experiment execution sheet pack (reporting.sheets.experiment)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SheetPack:
    """Template strings of the experiment execution sheet renderer.

    Format templates use ``str.format`` placeholders; the renderer
    escapes all values before substitution, so pack text may carry the
    literal ``&middot;`` entity of the HTML footer without being
    double-escaped.
    """

    #: Header banner: kind line, title, id labels.
    banner_kind: str
    title: str
    banner_run: str
    banner_dispatch: str
    #: ``html_document`` title of the sheet (``{run_id}``).
    doc_title_tpl: str
    #: Identity section and its row labels.
    section_identity: str
    label_project: str
    label_paper: str
    label_goal: str
    label_track: str
    label_goal_version: str
    label_package: str
    label_run: str
    label_dispatch: str
    label_dispatched_at: str
    #: Section titles 2..11.
    section_objective: str
    section_reagents: str
    section_instruments: str
    section_procedure: str
    #: Numbered step head (``{index}``).
    step_tpl: str
    no_items_recorded: str
    section_critical_controls: str
    section_prohibited: str
    none_recorded_in_package: str
    #: The STRICT-track emphasis of the prohibited block.
    strict_emphasis: str
    section_safety: str
    section_operator_record: str
    section_required_returns: str
    #: Checklist fill-in label (``returned as file``).
    returned_as_file_label: str
    section_additional_data: str
    #: Signature/date lines.
    operator_signature: str
    supervisor_signature: str
    #: Fixed print footer (``{version}``, ``{stamp}``) -- HTML entity
    #: form (``&middot;``) and the plain form the PDF writer consumes.
    footer_html_tpl: str
    footer_pdf_tpl: str


# ---------------------------------------------------------------------------
# Computation execution sheet pack (reporting.sheets.computation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComputationPack:
    """Template strings of the computation execution sheet renderer."""

    #: Header banner: kind line, title, id labels.
    banner_kind: str
    title: str
    banner_job: str
    banner_run: str
    #: ``html_document`` title of the sheet (``{job_id}``).
    doc_title_tpl: str
    section_identity_state: str
    label_job: str
    label_run: str
    label_backend: str
    label_state: str
    label_created_at: str
    label_goal: str
    #: Labels derived from durable-record field names, keyed by the
    #: label-ified name the renderer derives (``key.replace("_", " ")``);
    #: a label without an entry falls back to the derived name.
    field_labels: dict[str, str]
    section_inputs: str
    goal_not_registered_inputs: str
    no_inputs_recorded: str
    section_command: str
    label_working_directory: str
    section_resources: str
    label_modules: str
    module_load_header: str
    label_environment_overrides: str
    env_variable_header: str
    env_value_header: str
    no_modules_or_environment: str
    ssh_no_resources: str
    local_no_resources: str
    section_outputs: str
    label_collected_artifacts: str
    not_collected_yet: str
    label_artifact_id_rule: str
    #: The deterministic artifact-id rule line around the verbatim
    #: ``generate_id(...)`` call: ``{prefix} <span>rule</span>{suffix}``.
    artifact_id_rule_prefix: str
    artifact_id_rule_suffix: str
    output_header: str
    artifact_id_header: str
    section_validation: str
    label_acceptance_criteria: str
    not_registered_short: str
    #: Acceptance line (``{mode}``) and frozen marker (``{value}``).
    decision_mode_tpl: str
    frozen_tpl: str
    yes: str
    no: str
    label_statistical_design: str
    #: The present-case design label (carries the SS9 spec ref).
    design_ss9_label: str
    not_registered_ss9: str
    label_design: str
    label_primary_method: str
    label_metrics: str
    label_margin: str
    label_margin_basis: str
    label_alpha: str
    label_confidence_level: str
    label_preprocessing_rules: str
    label_outlier_rules: str
    label_failed_run_handling: str
    #: Fixed print footer (``{version}``, ``{stamp}``).
    footer_html_tpl: str


# ---------------------------------------------------------------------------
# Final reproduction report pack (reporting.pdf_report)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportPack:
    """Template strings of the deterministic PDF report renderer."""

    #: PDF document title and cover (``{project_id}`` etc.).
    doc_title_tpl: str
    title: str
    subtitle_tpl: str
    #: Section titles in render order (the report's section list).
    section_titles: tuple[str, ...]
    #: Executive summary.
    method_repro_tpl: str
    blocking_reasons: str
    finding_tpl: str
    no_critical_hit: str
    no_metrics_tpl: str
    ci_tpl: str
    ci_not_recorded: str
    band_tpl: str
    band_ref_tpl: str
    band_not_recorded: str
    headline_tpl: str
    #: Recovery ladder labels keyed by ``MethodReproducibility.value``
    #: (08-STRICT-RECOVERY-CLOSURE.md L1-L4 vocabulary).
    recovery_labels: dict[str, str]
    #: Simulation/real-data labels.
    data_label_mixed: str
    data_label_computation: str
    data_label_real: str
    data_label_none: str
    #: Target paper identity and reproduction scope.
    label_identity: str
    #: "Value" column of the identity table and of the analysis-results
    #: table of the core findings (one shared field).
    label_value: str
    label_doi: str
    label_title: str
    label_source_type: str
    label_project_phase: str
    label_current_plan_version: str
    label_project_id: str
    scope_label: str
    label_record: str
    label_count: str
    label_goals: str
    label_requirements: str
    label_inventory_items: str
    label_acceptance_criteria: str
    label_analysis_protocols: str
    label_statistical_designs: str
    label_closure_contracts: str
    frozen_acceptance_label: str
    label_acceptance: str
    label_goal: str
    label_frozen: str
    label_mode: str
    label_tolerance: str
    yes: str
    no: str
    no_acceptance: str
    #: Pipeline summary.
    label_stage: str
    label_recorded_state: str
    #: Pipeline stage labels (the rows of the pipeline-summary table).
    label_stage_research: str
    label_stage_inventory: str
    label_stage_planning: str
    label_stage_execution: str
    label_stage_analysis: str
    label_stage_artifacts: str
    pipeline_research_tpl: str
    pipeline_inventory_tpl: str
    pipeline_planning_tpl: str
    pipeline_execution_tpl: str
    pipeline_analysis_tpl: str
    pipeline_artifacts_tpl: str
    label_runs: str
    label_run: str
    label_type: str
    label_lifecycle: str
    label_review: str
    #: "Status" column of the runs table and of the frozen-plan table of
    #: the audit trail (one shared field).
    label_status: str
    no_runs: str
    #: Requirement outcomes.
    label_requirement: str
    label_statement: str
    label_criticality: str
    label_outcome: str
    label_method: str
    no_requirements: str
    runs_summary_tpl: str
    #: Core findings (per CRITICAL requirement).
    no_critical: str
    #: Per-requirement heading (``{requirement_id}``, ``{outcome}`` --
    #: frozen vocabulary, identical in every language).
    critical_heading_tpl: str
    analysis_results_label: str
    label_result: str
    label_analysis: str
    label_protocol: str
    label_metric: str
    no_results: str
    evidence_records_label: str
    label_evidence: str
    label_finding: str
    no_evidence: str
    decisions_label: str
    label_decision: str
    label_rationale: str
    no_decisions: str
    #: Governance exercised.
    recovery_ladder_label: str
    recovery_tpl: str
    recovered_tpl: str
    none: str
    closure_contracts_label: str
    label_closure: str
    label_allowed: str
    label_recovery_progress: str
    closure_progress_tpl: str
    no_closures: str
    designs_label: str
    label_design: str
    label_metrics: str
    label_n_policy: str
    label_margin: str
    label_basis: str
    label_alpha: str
    no_designs: str
    supervisor_decisions_label: str
    no_supervisor_decisions: str
    ac02_label: str
    revisions_tpl: str
    no_revisions: str
    reconciliations_label: str
    label_event: str
    label_timestamp: str
    label_actor: str
    label_transition: str
    label_reason: str
    no_reconciliations: str
    #: Audit trail.
    git_state_label: str
    git_state_tpl: str
    git_not_recorded: str
    frozen_plan_refs_label: str
    label_version: str
    label_frozen_at: str
    label_frozen_commit: str
    not_frozen: str
    no_plans: str
    checkpoint_label: str
    label_object: str
    label_checkpoint: str
    no_events: str
    artifacts_label: str
    label_artifact: str
    label_sha256: str
    label_size: str
    label_producer: str
    no_manifests: str
    #: Simulation and real-data labeling.
    label_label: str
    recorded_types_tpl: str
    recorded_items_tpl: str
    #: Table of contents.
    toc_title: str


# ---------------------------------------------------------------------------
# The packs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplatePack:
    """One language pack: every template string of the renderer family.

    ``language`` is the explicit input key (never detected), ``html_lang``
    the HTML document language tag, ``not_recorded`` /
    ``not_registered`` / ``deterministic_render`` the shared markers, and
    ``experiment`` / ``computation`` / ``report`` the per-renderer
    template strings.
    """

    language: str
    html_lang: str
    not_recorded: str
    not_registered: str
    deterministic_render: str
    experiment: SheetPack
    computation: ComputationPack
    report: ReportPack


#: The English pack -- byte-identical renderings to the pre-issue #122
#: renderers (every string matches the hardcoded English verbatim).
EN_PACK: Final[TemplatePack] = TemplatePack(
    language="en",
    html_lang="en",
    not_recorded="not recorded",
    not_registered="not registered in the project registry",
    deterministic_render="deterministic render",
    experiment=SheetPack(
        banner_kind="Operator execution sheet",
        title="Experiment Execution Sheet",
        banner_run="run",
        banner_dispatch="dispatch",
        doc_title_tpl="Experiment execution sheet — {run_id}",
        section_identity="Identity",
        label_project="Project",
        label_paper="Paper",
        label_goal="Goal",
        label_track="Track",
        label_goal_version="Goal version",
        label_package="Package",
        label_run="Run",
        label_dispatch="Dispatch",
        label_dispatched_at="Dispatched at",
        section_objective="Objective",
        section_reagents="Reagents",
        section_instruments="Instruments",
        section_procedure="Procedure",
        step_tpl="Step {index}",
        no_items_recorded="no items recorded",
        section_critical_controls="Critical control variables",
        section_prohibited="Prohibited changes",
        none_recorded_in_package="none recorded in the execution package",
        strict_emphasis=(
            "Strict reproduction track: every listed change is prohibited"
            " and requires a supervisor decision before it may be made"
        ),
        section_safety="Safety notes",
        section_operator_record="Operator record",
        section_required_returns="Required returns",
        returned_as_file_label="returned as file",
        section_additional_data="Additional package data",
        operator_signature="Operator signature & date",
        supervisor_signature="Supervisor signature & date",
        footer_html_tpl=(
            "scientific-reproduction &middot; experiment execution sheet"
            " v{version} &middot; {stamp}"
        ),
        footer_pdf_tpl=(
            "scientific-reproduction · experiment execution sheet"
            " v{version} · {stamp}"
        ),
    ),
    computation=ComputationPack(
        banner_kind="Operator execution sheet",
        title="Computation Execution Sheet",
        banner_job="job",
        banner_run="run",
        doc_title_tpl="Computation execution sheet — {job_id}",
        section_identity_state="Identity and job state",
        label_job="Job",
        label_run="Run",
        label_backend="Backend",
        label_state="State",
        label_created_at="Created at",
        label_goal="Goal",
        field_labels={
            "submitted at": "submitted at",
            "completed at": "completed at",
            "cancelled at": "cancelled at",
            "collected at": "collected at",
            "External id": "External id",
            "Remote pid": "Remote pid",
            "Scheduler state": "Scheduler state",
            "Pid": "Pid",
            "Exit code": "Exit code",
            "Failure class": "Failure class",
            "error": "error",
            "recovery note": "recovery note",
        },
        section_inputs="Inputs",
        goal_not_registered_inputs=(
            "goal not registered — inputs not recorded"
        ),
        no_inputs_recorded="no inputs recorded",
        section_command="Command",
        label_working_directory="Working directory",
        section_resources="Resource requests (Slurm/SSH parameters)",
        label_modules="Modules",
        module_load_header="module load",
        label_environment_overrides="Environment overrides",
        env_variable_header="variable",
        env_value_header="value",
        no_modules_or_environment=(
            "no modules or environment overrides recorded"
        ),
        ssh_no_resources=(
            "No resource requests are persisted by the ssh backend; the"
            " remote command is executed as recorded above."
        ),
        local_no_resources=(
            "No resource requests are persisted by the local backend; the"
            " command runs on the local host as recorded above."
        ),
        section_outputs="Required outputs and artifact naming",
        label_collected_artifacts="Collected artifacts",
        not_collected_yet="not collected yet",
        label_artifact_id_rule="Artifact id rule",
        artifact_id_rule_prefix="deterministic",
        artifact_id_rule_suffix=(
            " — the compute adapter's own naming (15-ADAPTER-SPEC.md SS3,"
            " AC-03)"
        ),
        output_header="output",
        artifact_id_header="artifact id",
        section_validation="Convergence and validation criteria",
        label_acceptance_criteria="Acceptance criteria",
        not_registered_short="not registered",
        decision_mode_tpl=" — decision mode {mode}",
        frozen_tpl=" (frozen: {value})",
        yes="yes",
        no="no",
        label_statistical_design="Statistical design",
        design_ss9_label="Statistical design (07-SS9)",
        not_registered_ss9="not registered (07-SS9)",
        label_design="Design",
        label_primary_method="Primary method",
        label_metrics="Metrics",
        label_margin="Margin",
        label_margin_basis="Margin basis",
        label_alpha="Alpha",
        label_confidence_level="Confidence level",
        label_preprocessing_rules="Preprocessing/exclusion rules",
        label_outlier_rules="Outlier rules",
        label_failed_run_handling="Failed-run handling",
        footer_html_tpl=(
            "scientific-reproduction &middot; computation execution sheet"
            " v{version} &middot; {stamp}"
        ),
    ),
    report=ReportPack(
        doc_title_tpl="Reproduction Report - {project_id}",
        title="Reproduction Report",
        subtitle_tpl=(
            "Project {project_id} - generated {generated_at} - report"
            " version {version}"
        ),
        section_titles=(
            "Executive summary",
            "Target paper identity and reproduction scope",
            "Pipeline summary",
            "Requirement outcomes",
            "Core findings",
            "Governance exercised",
            "Audit trail",
            "Simulation and real-data labeling",
        ),
        method_repro_tpl=(
            "Method reproducibility: {value} (ruleset {ruleset}, rule"
            " {rule})."
        ),
        blocking_reasons=" Blocking reasons: ",
        finding_tpl=(
            "Recorded outcomes across {total} requirements: {by_outcome}."
            " runs: {succeeded} succeeded, {failed} failed, {unresolved}"
            " unresolved."
        ),
        no_critical_hit=(
            "No recorded analysis result references a CRITICAL requirement."
        ),
        no_metrics_tpl=(
            "{result_id}: no recorded metrics (analysis ran without a"
            " metrics record)."
        ),
        ci_tpl=" ({confidence}% CI {lower} to {upper})",
        ci_not_recorded=" (confidence interval not recorded)",
        band_tpl=" vs frozen acceptance band +/-{tolerance}",
        band_ref_tpl=" ({band_ref})",
        band_not_recorded=" (acceptance band not recorded)",
        headline_tpl=(
            "Most important number: {name} = {value}{interval}{band}"
            " ({result_id}, protocol {protocol_version})."
        ),
        recovery_labels={
            "DIRECTLY_REPRODUCIBLE": "L1 direct",
            "REPRODUCIBLE_WITH_MINOR_RECOVERY": "L1/L2 minor recovery",
            "REPRODUCIBLE_WITH_METHOD_ADJUSTMENT": "L3 method adjustment",
            "ONLY_REPRODUCIBLE_AFTER_REDESIGN": "L4 redesign",
            "NOT_REPRODUCIBLE": "not reproducible",
            "UNDETERMINED": "undetermined",
            "INCONCLUSIVE": "inconclusive",
        },
        data_label_mixed=(
            "mixed: real experimental data and computation/simulation"
        ),
        data_label_computation="simulation/computation",
        data_label_real="real experimental data",
        data_label_none="no inventory recorded",
        label_identity="Identity",
        label_value="Value",
        label_doi="DOI",
        label_title="Title",
        label_source_type="Source type",
        label_project_phase="Project phase",
        label_current_plan_version="Current plan version",
        label_project_id="Project id",
        scope_label="Reproduction scope:",
        label_record="Record",
        label_count="Count",
        label_goals="Goals",
        label_requirements="Requirements",
        label_inventory_items="Inventory items",
        label_acceptance_criteria="Acceptance criteria",
        label_analysis_protocols="Analysis protocols",
        label_statistical_designs="Statistical designs",
        label_closure_contracts="Closure contracts",
        frozen_acceptance_label="Frozen acceptance criteria:",
        label_acceptance="Acceptance",
        label_goal="Goal",
        label_frozen="Frozen",
        label_mode="Mode",
        label_tolerance="Tolerance",
        yes="yes",
        no="no",
        no_acceptance="no recorded acceptance criteria.",
        label_stage="Stage",
        label_recorded_state="Recorded state",
        label_stage_research="Research",
        label_stage_inventory="Inventory",
        label_stage_planning="Planning",
        label_stage_execution="Execution",
        label_stage_analysis="Analysis",
        label_stage_artifacts="Artifacts",
        pipeline_research_tpl="{sources} sources, {evidence} evidence records",
        pipeline_inventory_tpl="{items} items, {requirements} requirements",
        pipeline_planning_tpl=(
            "{goals} goals, {acceptance} acceptance criteria, {protocols}"
            " protocols, {designs} designs, {closures} closure contracts"
        ),
        pipeline_execution_tpl=(
            "{runs} runs ({succeeded} succeeded, {failed} failed,"
            " {unresolved} unresolved)"
        ),
        pipeline_analysis_tpl="{results} result packages",
        pipeline_artifacts_tpl="{manifests} manifests",
        label_runs="Runs:",
        label_run="Run",
        label_type="Type",
        label_lifecycle="Lifecycle",
        label_review="Review",
        label_status="Status",
        no_runs="no recorded runs.",
        label_requirement="Requirement",
        label_statement="Statement",
        label_criticality="Criticality",
        label_outcome="Outcome",
        label_method="Method",
        no_requirements="no recorded requirements.",
        runs_summary_tpl=(
            "runs ({total} total): {succeeded} succeeded, {failed} failed,"
            " {unresolved} unresolved."
        ),
        no_critical="No CRITICAL requirements recorded.",
        critical_heading_tpl="{requirement_id} - {outcome}",
        analysis_results_label="Analysis results:",
        label_result="Result",
        label_analysis="Analysis",
        label_protocol="Protocol",
        label_metric="Metric",
        no_results="no recorded analysis results.",
        evidence_records_label="Evidence records:",
        label_evidence="Evidence",
        label_finding="Finding",
        no_evidence="no recorded evidence records.",
        decisions_label="Decisions:",
        label_decision="Decision",
        label_rationale="Rationale",
        no_decisions="no recorded decisions.",
        recovery_ladder_label="Recovery ladder:",
        recovery_tpl=(
            "Recorded method reproducibility: {value} -> {label} (ruleset"
            " {ruleset})."
        ),
        recovered_tpl=(
            "Recovered requirements: {requirements}. Recovery goals:"
            " {goals}. Method redesign goals: {redesign}."
        ),
        none="none",
        closure_contracts_label="Closure contracts:",
        label_closure="Closure",
        label_allowed="Allowed",
        label_recovery_progress="Recovery progress",
        closure_progress_tpl=(
            "eligible {eligible}, tested or ruled out {tested}, remaining"
            " {remaining}"
        ),
        no_closures="no recorded closure contracts.",
        designs_label="Statistical designs (recorded n/margin decisions):",
        label_design="Design",
        label_metrics="Metrics",
        label_n_policy="n policy",
        label_margin="Margin",
        label_basis="Basis",
        label_alpha="Alpha",
        no_designs="no recorded statistical designs.",
        supervisor_decisions_label="Supervisor decisions:",
        no_supervisor_decisions="No recorded supervisor decisions.",
        ac02_label="AC-02 collection rejections:",
        revisions_tpl=(
            "{count} recorded revision/rejection decisions: {ids}."
        ),
        no_revisions="no recorded collection rejections.",
        reconciliations_label="Monitor reconciliations:",
        label_event="Event",
        label_timestamp="Timestamp",
        label_actor="Actor",
        label_transition="Transition",
        label_reason="Reason",
        no_reconciliations="No recorded reconciliation events.",
        git_state_label="Git state:",
        git_state_tpl="HEAD {head}, {commits} commits.",
        git_not_recorded=(
            "Git state not recorded (workspace is not a git repository)."
        ),
        frozen_plan_refs_label="Frozen plan refs:",
        label_version="Version",
        label_frozen_at="Frozen at",
        label_frozen_commit="Frozen commit",
        not_frozen="not frozen",
        no_plans="no recorded plans.",
        checkpoint_label="Checkpoint events:",
        label_object="Object",
        label_checkpoint="Checkpoint",
        no_events="no recorded events.",
        artifacts_label="Artifact manifests:",
        label_artifact="Artifact",
        label_sha256="SHA-256",
        label_size="Size",
        label_producer="Producer",
        no_manifests="no recorded artifact manifests.",
        label_label="Label:",
        recorded_types_tpl="Recorded inventory item types: {types}.",
        recorded_items_tpl="Recorded inventory items: {items}.",
        toc_title="Table of contents",
    ),
)


#: The Chinese (simplified) pack.
ZH_PACK: Final[TemplatePack] = TemplatePack(
    language="zh",
    html_lang="zh",
    not_recorded="未记录",
    not_registered="未在项目注册表中注册",
    deterministic_render="确定性渲染",
    experiment=SheetPack(
        banner_kind="操作员执行单",
        title="实验执行单",
        banner_run="运行",
        banner_dispatch="派发",
        doc_title_tpl="实验执行单 — {run_id}",
        section_identity="身份",
        label_project="项目",
        label_paper="论文",
        label_goal="目标",
        label_track="轨道",
        label_goal_version="目标版本",
        label_package="包",
        label_run="运行",
        label_dispatch="派发",
        label_dispatched_at="派发时间",
        section_objective="目标说明",
        section_reagents="试剂",
        section_instruments="仪器",
        section_procedure="操作步骤",
        step_tpl="步骤 {index}",
        no_items_recorded="未记录条目",
        section_critical_controls="关键控制变量",
        section_prohibited="禁止变更",
        none_recorded_in_package="执行包中未记录",
        strict_emphasis=(
            "严格复现轨道: 所列出的每项变更均被禁止, 未经主管决策不得进行"
        ),
        section_safety="安全须知",
        section_operator_record="操作员记录",
        section_required_returns="必需返回项",
        returned_as_file_label="以文件返回",
        section_additional_data="附加包数据",
        operator_signature="操作员签名与日期",
        supervisor_signature="主管签名与日期",
        footer_html_tpl=(
            "scientific-reproduction &middot; 实验执行单 v{version}"
            " &middot; {stamp}"
        ),
        footer_pdf_tpl=(
            "scientific-reproduction · 实验执行单 v{version} · {stamp}"
        ),
    ),
    computation=ComputationPack(
        banner_kind="操作员执行单",
        title="计算执行单",
        banner_job="作业",
        banner_run="运行",
        doc_title_tpl="计算执行单 — {job_id}",
        section_identity_state="身份与作业状态",
        label_job="作业",
        label_run="运行",
        label_backend="后端",
        label_state="状态",
        label_created_at="创建时间",
        label_goal="目标",
        field_labels={
            "submitted at": "提交时间",
            "completed at": "完成时间",
            "cancelled at": "取消时间",
            "collected at": "收集时间",
            "External id": "外部 id",
            "Remote pid": "远程 pid",
            "Scheduler state": "调度器状态",
            "Pid": "Pid",
            "Exit code": "退出码",
            "Failure class": "故障类别",
            "error": "错误",
            "recovery note": "恢复说明",
        },
        section_inputs="输入",
        goal_not_registered_inputs="目标未注册 — 未记录输入",
        no_inputs_recorded="未记录输入",
        section_command="命令",
        label_working_directory="工作目录",
        section_resources="资源请求 (Slurm/SSH 参数)",
        label_modules="模块",
        module_load_header="模块加载",
        label_environment_overrides="环境覆盖",
        env_variable_header="变量",
        env_value_header="值",
        no_modules_or_environment="未记录模块或环境覆盖",
        ssh_no_resources=(
            "ssh 后端不持久化资源请求; 远程命令按以上记录执行."
        ),
        local_no_resources=(
            "local 后端不持久化资源请求; 命令按以上记录在本地主机上运行."
        ),
        section_outputs="必需输出与工件命名",
        label_collected_artifacts="已收集工件",
        not_collected_yet="尚未收集",
        label_artifact_id_rule="工件 id 规则",
        artifact_id_rule_prefix="确定性",
        artifact_id_rule_suffix=(
            " — 计算适配器自身的命名规则 (15-ADAPTER-SPEC.md SS3, AC-03)"
        ),
        output_header="输出",
        artifact_id_header="工件 id",
        section_validation="收敛与验证标准",
        label_acceptance_criteria="验收标准",
        not_registered_short="未注册",
        decision_mode_tpl=" — 决策模式 {mode}",
        frozen_tpl=" (冻结: {value})",
        yes="是",
        no="否",
        label_statistical_design="统计设计",
        design_ss9_label="统计设计 (07-SS9)",
        not_registered_ss9="未注册 (07-SS9)",
        label_design="设计",
        label_primary_method="主要方法",
        label_metrics="指标",
        label_margin="裕量",
        label_margin_basis="裕量依据",
        label_alpha="α",
        label_confidence_level="置信水平",
        label_preprocessing_rules="预处理/排除规则",
        label_outlier_rules="离群值规则",
        label_failed_run_handling="失败运行处理",
        footer_html_tpl=(
            "scientific-reproduction &middot; 计算执行单 v{version}"
            " &middot; {stamp}"
        ),
    ),
    report=ReportPack(
        doc_title_tpl="复现报告 - {project_id}",
        title="复现报告",
        subtitle_tpl=(
            "项目 {project_id} - 生成于 {generated_at} - 报告版本 {version}"
        ),
        section_titles=(
            "执行摘要",
            "目标论文身份与复现范围",
            "流水线摘要",
            "需求结果",
            "核心发现",
            "治理行使",
            "审计追踪",
            "模拟与真实数据标注",
        ),
        method_repro_tpl="方法可复现性: {value} (规则集 {ruleset}, 规则 {rule}).",
        blocking_reasons=" 阻断原因: ",
        finding_tpl=(
            "记录的需求结果 {total} 项: {by_outcome}. 运行: {succeeded}"
            " 成功, {failed} 失败, {unresolved} 未解决."
        ),
        no_critical_hit="没有记录的分析结果引用关键(CRITICAL)需求.",
        no_metrics_tpl="{result_id}: 无记录的指标 (分析运行但未记录指标).",
        ci_tpl=" ({confidence}% 置信区间 {lower} 至 {upper})",
        ci_not_recorded=" (未记录置信区间)",
        band_tpl=" 对照冻结验收带 +/-{tolerance}",
        band_ref_tpl=" ({band_ref})",
        band_not_recorded=" (未记录验收带)",
        headline_tpl=(
            "最重要数值: {name} = {value}{interval}{band} ({result_id},"
            " 协议 {protocol_version})."
        ),
        recovery_labels={
            "DIRECTLY_REPRODUCIBLE": "L1 直接复现",
            "REPRODUCIBLE_WITH_MINOR_RECOVERY": "L1/L2 轻微恢复",
            "REPRODUCIBLE_WITH_METHOD_ADJUSTMENT": "L3 方法调整",
            "ONLY_REPRODUCIBLE_AFTER_REDESIGN": "L4 重新设计",
            "NOT_REPRODUCIBLE": "不可复现",
            "UNDETERMINED": "未确定",
            "INCONCLUSIVE": "无定论",
        },
        data_label_mixed="混合: 真实实验数据与计算/模拟",
        data_label_computation="模拟/计算",
        data_label_real="真实实验数据",
        data_label_none="未记录库存条目",
        label_identity="身份",
        label_value="值",
        label_doi="DOI",
        label_title="标题",
        label_source_type="来源类型",
        label_project_phase="项目阶段",
        label_current_plan_version="当前计划版本",
        label_project_id="项目 id",
        scope_label="复现范围:",
        label_record="记录",
        label_count="数量",
        label_goals="目标",
        label_requirements="需求",
        label_inventory_items="库存条目",
        label_acceptance_criteria="验收标准",
        label_analysis_protocols="分析协议",
        label_statistical_designs="统计设计",
        label_closure_contracts="闭环合约",
        frozen_acceptance_label="冻结验收标准:",
        label_acceptance="验收",
        label_goal="目标",
        label_frozen="冻结",
        label_mode="模式",
        label_tolerance="容差",
        yes="是",
        no="否",
        no_acceptance="未记录验收标准.",
        label_stage="阶段",
        label_recorded_state="记录状态",
        label_stage_research="研究",
        label_stage_inventory="清单",
        label_stage_planning="规划",
        label_stage_execution="执行",
        label_stage_analysis="分析",
        label_stage_artifacts="工件",
        pipeline_research_tpl="{sources} 个来源, {evidence} 条证据记录",
        pipeline_inventory_tpl="{items} 个条目, {requirements} 项需求",
        pipeline_planning_tpl=(
            "{goals} 个目标, {acceptance} 条验收标准, {protocols} 个协议,"
            " {designs} 项设计, {closures} 份闭环合约"
        ),
        pipeline_execution_tpl=(
            "{runs} 次运行 ({succeeded} 成功, {failed} 失败, {unresolved}"
            " 未解决)"
        ),
        pipeline_analysis_tpl="{results} 个结果包",
        pipeline_artifacts_tpl="{manifests} 份清单",
        label_runs="运行:",
        label_run="运行",
        label_type="类型",
        label_lifecycle="生命周期",
        label_review="评审",
        label_status="状态",
        no_runs="未记录运行.",
        label_requirement="需求",
        label_statement="陈述",
        label_criticality="关键度",
        label_outcome="结果",
        label_method="方法",
        no_requirements="未记录需求.",
        runs_summary_tpl=(
            "运行 ({total} 次总计): {succeeded} 成功, {failed} 失败,"
            " {unresolved} 未解决."
        ),
        no_critical="未记录关键(CRITICAL)需求.",
        critical_heading_tpl="{requirement_id} - {outcome}",
        analysis_results_label="分析结果:",
        label_result="结果",
        label_analysis="分析",
        label_protocol="协议",
        label_metric="指标",
        no_results="未记录分析结果.",
        evidence_records_label="证据记录:",
        label_evidence="证据",
        label_finding="发现",
        no_evidence="未记录证据记录.",
        decisions_label="决策:",
        label_decision="决策",
        label_rationale="理由",
        no_decisions="未记录决策.",
        recovery_ladder_label="恢复阶梯:",
        recovery_tpl="记录的方法可复现性: {value} -> {label} (规则集 {ruleset}).",
        recovered_tpl=(
            "已恢复需求: {requirements}. 恢复目标: {goals}. 方法重新设计"
            "目标: {redesign}."
        ),
        none="无",
        closure_contracts_label="闭环合约:",
        label_closure="闭环",
        label_allowed="允许",
        label_recovery_progress="恢复进度",
        closure_progress_tpl=(
            "符合资格 {eligible}, 已测试或排除 {tested}, 剩余 {remaining}"
        ),
        no_closures="未记录闭环合约.",
        designs_label="统计设计 (记录的 n/裕量决策):",
        label_design="设计",
        label_metrics="指标",
        label_n_policy="n 政策",
        label_margin="裕量",
        label_basis="依据",
        label_alpha="α",
        no_designs="未记录统计设计.",
        supervisor_decisions_label="主管决策:",
        no_supervisor_decisions="未记录主管决策.",
        ac02_label="AC-02 集合驳回:",
        revisions_tpl="记录了 {count} 条修订/驳回决策: {ids}.",
        no_revisions="未记录集合驳回.",
        reconciliations_label="监控对账:",
        label_event="事件",
        label_timestamp="时间戳",
        label_actor="行为者",
        label_transition="转换",
        label_reason="原因",
        no_reconciliations="未记录对账事件.",
        git_state_label="Git 状态:",
        git_state_tpl="HEAD {head}, {commits} 次提交.",
        git_not_recorded="未记录 Git 状态 (工作区不是 git 仓库).",
        frozen_plan_refs_label="冻结计划引用:",
        label_version="版本",
        label_frozen_at="冻结时间",
        label_frozen_commit="冻结提交",
        not_frozen="未冻结",
        no_plans="未记录计划.",
        checkpoint_label="检查点事件:",
        label_object="对象",
        label_checkpoint="检查点",
        no_events="未记录事件.",
        artifacts_label="工件清单:",
        label_artifact="工件",
        label_sha256="SHA-256",
        label_size="大小",
        label_producer="生产者",
        no_manifests="未记录工件清单.",
        label_label="标注:",
        recorded_types_tpl="记录的库存条目类型: {types}.",
        recorded_items_tpl="记录的库存条目: {items}.",
        toc_title="目录",
    ),
)


#: Language key -> pack (the resolution table; explicit input only).
_PACKS: Final[dict[str, TemplatePack]] = {
    EN_PACK.language: EN_PACK,
    ZH_PACK.language: ZH_PACK,
}


def resolve_pack(language: str) -> TemplatePack:
    """Resolve the explicit ``language`` key to its :class:`TemplatePack`.

    The language is always an explicit renderer input -- there is no
    locale auto-detection anywhere -- so ``(state, language)`` maps to
    byte-identical output for identical inputs (the determinism
    guarantee of ``14-STATE-GIT-ARTIFACTS.md`` SS7).

    Raises:
        TypeError: ``language`` is not a non-empty string.
        ValueError: the language has no shipped pack (stable message
            listing :data:`AVAILABLE_LANGUAGES`).
    """
    if not isinstance(language, str) or not language.strip():
        raise TypeError(
            f"language must be a non-empty string, got {language!r}"
        )
    if language not in _PACKS:
        available = ", ".join(AVAILABLE_LANGUAGES)
        raise ValueError(
            f"unknown render language {language!r}; available languages:"
            f" {available}"
        )
    return _PACKS[language]
