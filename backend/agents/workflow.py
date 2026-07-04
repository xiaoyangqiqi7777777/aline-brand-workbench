from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from backend.agents.errors import InvalidModelOutputError
from backend.agents.ports import (
    ArtifactWriter,
    InvocationRecorder,
    InvocationStatus,
    ModelInvocationRecord,
)
from backend.agents.prompts import build_model_messages
from backend.agents.schemas.brand_spec import BrandSpec
from backend.agents.schemas.directions import (
    Direction,
    DirectionDraftOutput,
    DirectionOutput,
)
from backend.agents.schemas.intake import IntakeOutput, IntakeResumePayload
from backend.agents.schemas.ip import IPDraft, IPOutput
from backend.agents.schemas.logo import LogoConcept, LogoDraftOutput, LogoOutput
from backend.agents.schemas.materials import (
    MaterialDraftOutput,
    MaterialOutput,
    MaterialScene,
)
from backend.agents.schemas.proposal import (
    ProposalOutput,
    ProposalSection,
    ProposalSectionType,
)
from backend.agents.schemas.review import ReviewOutput
from backend.agents.schemas.vi import VIOutput
from backend.agents.schemas.workflow_controls import (
    ConfirmStageDecision,
    IPChoice,
    IPChoiceAction,
    ReviewDecision,
    SelectItemDecision,
)
from backend.providers.models.base import (
    ImageGenerationRequest,
    ImageModelProvider,
    ModelCapability,
    TextGenerationRequest,
    TextModelProvider,
)


class BrandWorkflowState(TypedDict, total=False):
    project_id: str
    brand_spec: dict[str, Any]
    status: str
    intake_output: dict[str, Any]
    direction_output: dict[str, Any]
    selected_direction_id: str
    logo_output: dict[str, Any]
    selected_logo_id: str
    vi_output: dict[str, Any]
    ip_skipped: bool
    ip_output: dict[str, Any]
    material_output: dict[str, Any]
    review_output: dict[str, Any]
    proposal_output: dict[str, Any]
    selected_version_ids: dict[str, str]


def build_brand_workflow(
    *,
    text_provider: TextModelProvider,
    image_provider: ImageModelProvider,
    artifact_writer: ArtifactWriter,
    invocation_recorder: InvocationRecorder,
    checkpointer: Any,
    interrupt_before: Sequence[str] = (),
) -> Any:
    """Build the deterministic Brand Agent graph around human interrupts.

    Business persistence is deliberately absent. The application layer owns
    stage versions, decisions, outbox delivery, and the production checkpointer.
    """

    def generate_text(
        state: BrandWorkflowState,
        *,
        capability: ModelCapability,
        payload: dict[str, Any],
        output_model: Any,
        prompt_version: str,
    ) -> Any:
        request = TextGenerationRequest(
            request_id=f"{state['project_id']}:{capability.value.lower()}",
            capability=capability,
            prompt_version=prompt_version,
            messages=build_model_messages(capability, payload),
            json_schema=output_model.model_json_schema(),
        )
        try:
            result = text_provider.generate_structured(request)
        except Exception as error:
            invocation_recorder.record_model_invocation(
                ModelInvocationRecord(
                    request_id=request.request_id,
                    capability=capability,
                    prompt_version=request.prompt_version,
                    provider=getattr(text_provider, "provider_name", "unknown"),
                    model=getattr(text_provider, "model_name", "unknown"),
                    status=InvocationStatus.FAILED,
                    error_code=getattr(error, "code", "PROVIDER_UNEXPECTED"),
                )
            )
            raise
        try:
            validated = output_model.model_validate(result.content_json)
            invocation_recorder.record_model_invocation(
                ModelInvocationRecord(
                    request_id=request.request_id,
                    capability=capability,
                    prompt_version=request.prompt_version,
                    provider=result.provider,
                    model=result.model,
                    status=InvocationStatus.SUCCEEDED,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    latency_ms=result.latency_ms,
                )
            )
            return validated
        except ValidationError as first_error:
            invocation_recorder.record_model_invocation(
                ModelInvocationRecord(
                    request_id=request.request_id,
                    capability=capability,
                    prompt_version=request.prompt_version,
                    provider=result.provider,
                    model=result.model,
                    status=InvocationStatus.INVALID_OUTPUT,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    latency_ms=result.latency_ms,
                    error_code="INVALID_MODEL_OUTPUT",
                )
            )
            repair_request = TextGenerationRequest(
                request_id=f"{request.request_id}:repair",
                capability=capability,
                prompt_version=f"{prompt_version}-repair",
                messages=build_model_messages(
                    capability,
                    payload,
                    repair_errors=first_error.errors(
                        include_url=False,
                        include_context=False,
                        include_input=False,
                    ),
                    invalid_output=result.content_json,
                ),
                json_schema=output_model.model_json_schema(),
            )
            try:
                repaired = text_provider.generate_structured(repair_request)
            except Exception as error:
                invocation_recorder.record_model_invocation(
                    ModelInvocationRecord(
                        request_id=repair_request.request_id,
                        capability=capability,
                        prompt_version=repair_request.prompt_version,
                        provider=getattr(text_provider, "provider_name", "unknown"),
                        model=getattr(text_provider, "model_name", "unknown"),
                        status=InvocationStatus.FAILED,
                        error_code=getattr(error, "code", "PROVIDER_UNEXPECTED"),
                    )
                )
                raise
            try:
                validated = output_model.model_validate(repaired.content_json)
                invocation_recorder.record_model_invocation(
                    ModelInvocationRecord(
                        request_id=repair_request.request_id,
                        capability=capability,
                        prompt_version=repair_request.prompt_version,
                        provider=repaired.provider,
                        model=repaired.model,
                        status=InvocationStatus.SUCCEEDED,
                        input_tokens=repaired.input_tokens,
                        output_tokens=repaired.output_tokens,
                        latency_ms=repaired.latency_ms,
                    )
                )
                return validated
            except ValidationError as second_error:
                invocation_recorder.record_model_invocation(
                    ModelInvocationRecord(
                        request_id=repair_request.request_id,
                        capability=capability,
                        prompt_version=repair_request.prompt_version,
                        provider=repaired.provider,
                        model=repaired.model,
                        status=InvocationStatus.INVALID_OUTPUT,
                        input_tokens=repaired.input_tokens,
                        output_tokens=repaired.output_tokens,
                        latency_ms=repaired.latency_ms,
                        error_code="INVALID_MODEL_OUTPUT",
                    )
                )
                raise InvalidModelOutputError(
                    f"{capability.value} output remained invalid after one repair"
                ) from second_error

    def generate_one_image(
        state: BrandWorkflowState,
        *,
        item_kind: str,
        item_id: str,
        prompt: str,
        reference_asset_ids: list[str] | None = None,
    ) -> UUID:
        image_request_id = f"{state['project_id']}:{item_kind}:{item_id}"
        request = ImageGenerationRequest(
            request_id=image_request_id,
            prompt=prompt,
            reference_artifact_ids=reference_asset_ids or [],
            count=1,
        )
        image_capability = {
            "direction": ModelCapability.DIRECTIONS,
            "logo": ModelCapability.LOGO,
            "ip": ModelCapability.IP,
            "material": ModelCapability.MATERIALS,
        }[item_kind]
        try:
            images = image_provider.generate(request)
        except Exception as error:
            invocation_recorder.record_model_invocation(
                ModelInvocationRecord(
                    request_id=request.request_id,
                    capability=image_capability,
                    prompt_version=f"{item_kind}-image-v1",
                    provider=getattr(image_provider, "provider_name", "unknown"),
                    model=getattr(image_provider, "model_name", "unknown"),
                    status=InvocationStatus.FAILED,
                    error_code=getattr(error, "code", "PROVIDER_UNEXPECTED"),
                )
            )
            raise
        if len(images) != 1:
            invocation_recorder.record_model_invocation(
                ModelInvocationRecord(
                    request_id=request.request_id,
                    capability=image_capability,
                    prompt_version=f"{item_kind}-image-v1",
                    provider=getattr(image_provider, "provider_name", "unknown"),
                    model=getattr(image_provider, "model_name", "unknown"),
                    status=InvocationStatus.FAILED,
                    image_count=len(images),
                    error_code="INVALID_IMAGE_COUNT",
                )
            )
            raise ValueError(f"{item_kind} item must produce exactly one image")
        invocation_recorder.record_model_invocation(
            ModelInvocationRecord(
                request_id=request.request_id,
                capability=image_capability,
                prompt_version=f"{item_kind}-image-v1",
                provider=images[0].provider,
                model=images[0].model,
                status=InvocationStatus.SUCCEEDED,
                image_count=1,
                latency_ms=images[0].latency_ms,
            )
        )
        stored = artifact_writer.store_generated_image(
            request_id=image_request_id,
            image=images[0],
        )
        return stored.artifact_id

    def generate_image_batch(
        state: BrandWorkflowState,
        *,
        item_kind: str,
        items: Sequence[Any],
        prompt_for: Callable[[Any], str],
        references_for: Callable[[Any], list[str]] | None = None,
    ) -> list[UUID]:
        artifact_ids: list[UUID] = []
        try:
            for item in items:
                artifact_ids.append(
                    generate_one_image(
                        state,
                        item_kind=item_kind,
                        item_id=item.id,
                        prompt=prompt_for(item),
                        reference_asset_ids=(references_for(item) if references_for else None),
                    )
                )
        except Exception:
            artifact_writer.discard_temporary_artifacts(artifact_ids)
            raise
        return artifact_ids

    def with_version(
        state: BrandWorkflowState,
        stage: str,
        version_id: UUID,
    ) -> dict[str, str]:
        versions = dict(state.get("selected_version_ids", {}))
        versions[stage] = str(version_id)
        return versions

    def analyze_intake(state: BrandWorkflowState) -> BrandWorkflowState:
        brand_spec = BrandSpec.model_validate(state["brand_spec"])
        intake_output = generate_text(
            state,
            capability=ModelCapability.INTAKE,
            payload={"brand_spec": brand_spec.model_dump(mode="json")},
            output_model=IntakeOutput,
            prompt_version="intake-v1",
        )
        return {
            "intake_output": intake_output.model_dump(mode="json"),
            "status": "DIRECTIONS" if intake_output.ready else "NEEDS_INPUT",
        }

    def route_after_intake(
        state: BrandWorkflowState,
    ) -> Literal["await_intake_answers", "generate_directions"]:
        intake_output = IntakeOutput.model_validate(state["intake_output"])
        return "generate_directions" if intake_output.ready else "await_intake_answers"

    def await_intake_answers(state: BrandWorkflowState) -> BrandWorkflowState:
        intake_output = IntakeOutput.model_validate(state["intake_output"])
        resume_value = interrupt(
            {
                "kind": "intake_questions",
                "questions": [
                    question.model_dump(mode="json") for question in intake_output.questions
                ],
                "conflicts": [
                    conflict.model_dump(mode="json") for conflict in intake_output.conflicts
                ],
            }
        )
        resume_payload = IntakeResumePayload.model_validate(resume_value)
        brand_spec = BrandSpec.model_validate(state["brand_spec"])
        updated_spec = brand_spec.apply_user_answers(
            [(answer.field_path, answer.value) for answer in resume_payload.answers],
            source_id=f"{state['project_id']}:intake-resume",
        )
        return {
            "brand_spec": updated_spec.model_dump(mode="json"),
            "status": "INTAKE",
        }

    def generate_directions(state: BrandWorkflowState) -> BrandWorkflowState:
        brand_spec = BrandSpec.model_validate(state["brand_spec"])
        draft_output = generate_text(
            state,
            capability=ModelCapability.DIRECTIONS,
            payload={"brand_spec": brand_spec.model_dump(mode="json")},
            output_model=DirectionDraftOutput,
            prompt_version="directions-v1",
        )
        asset_ids = generate_image_batch(
            state,
            item_kind="direction",
            items=draft_output.directions,
            prompt_for=lambda draft: draft.image_prompt,
        )
        directions = [
            Direction(
                **draft.model_dump(),
                preview_asset_id=asset_id,
            )
            for draft, asset_id in zip(draft_output.directions, asset_ids, strict=True)
        ]
        output = DirectionOutput(brief=draft_output.brief, directions=directions)
        return {
            "direction_output": output.model_dump(mode="json"),
            "status": "WAITING_USER",
        }

    def await_direction_decision(state: BrandWorkflowState) -> BrandWorkflowState:
        output = DirectionOutput.model_validate(state["direction_output"])
        decision = SelectItemDecision.model_validate(
            interrupt(
                {
                    "kind": "direction_decision",
                    "direction_output": output.model_dump(mode="json"),
                }
            )
        )
        if decision.selected_item_id not in {item.id for item in output.directions}:
            raise ValueError("Selected direction does not exist in current output")
        return {
            "selected_direction_id": decision.selected_item_id,
            "selected_version_ids": with_version(state, "DIRECTIONS", decision.version_id),
            "status": "LOGO",
        }

    def generate_logo(state: BrandWorkflowState) -> BrandWorkflowState:
        brand_spec = BrandSpec.model_validate(state["brand_spec"])
        directions = DirectionOutput.model_validate(state["direction_output"])
        selected = next(
            item for item in directions.directions if item.id == state["selected_direction_id"]
        )
        drafts = generate_text(
            state,
            capability=ModelCapability.LOGO,
            payload={
                "brand_spec": brand_spec.model_dump(mode="json"),
                "selected_direction": selected.model_dump(mode="json"),
            },
            output_model=LogoDraftOutput,
            prompt_version="logo-v1",
        )
        asset_ids = generate_image_batch(
            state,
            item_kind="logo",
            items=drafts.concepts,
            prompt_for=lambda draft: draft.image_prompt,
            references_for=lambda _: [str(selected.preview_asset_id)],
        )
        concepts = [
            LogoConcept(
                **draft.model_dump(),
                preview_asset_id=asset_id,
            )
            for draft, asset_id in zip(drafts.concepts, asset_ids, strict=True)
        ]
        return {
            "logo_output": LogoOutput(concepts=concepts).model_dump(mode="json"),
            "status": "WAITING_USER",
        }

    def await_logo_decision(state: BrandWorkflowState) -> BrandWorkflowState:
        output = LogoOutput.model_validate(state["logo_output"])
        decision = SelectItemDecision.model_validate(
            interrupt(
                {
                    "kind": "logo_decision",
                    "logo_output": output.model_dump(mode="json"),
                }
            )
        )
        if decision.selected_item_id not in {item.id for item in output.concepts}:
            raise ValueError("Selected logo does not exist in current output")
        return {
            "selected_logo_id": decision.selected_item_id,
            "selected_version_ids": with_version(state, "LOGO", decision.version_id),
            "status": "VI",
        }

    def generate_vi(state: BrandWorkflowState) -> BrandWorkflowState:
        output = LogoOutput.model_validate(state["logo_output"])
        selected_logo = next(
            item for item in output.concepts if item.id == state["selected_logo_id"]
        )
        vi_output = generate_text(
            state,
            capability=ModelCapability.VI,
            payload={"selected_logo": selected_logo.model_dump(mode="json")},
            output_model=VIOutput,
            prompt_version="vi-v1",
        )
        return {
            "vi_output": vi_output.model_dump(mode="json"),
            "status": "WAITING_USER",
        }

    def await_vi_decision(state: BrandWorkflowState) -> BrandWorkflowState:
        output = VIOutput.model_validate(state["vi_output"])
        decision = ConfirmStageDecision.model_validate(
            interrupt(
                {
                    "kind": "vi_decision",
                    "vi_output": output.model_dump(mode="json"),
                }
            )
        )
        return {
            "selected_version_ids": with_version(state, "VI", decision.version_id),
            "status": "IP_CHOICE",
        }

    def await_ip_choice(state: BrandWorkflowState) -> BrandWorkflowState:
        choice = IPChoice.model_validate(
            interrupt(
                {
                    "kind": "ip_choice",
                    "message": "生成 1 个品牌 IP，或跳过并继续物料。",
                }
            )
        )
        skipped = choice.action is IPChoiceAction.SKIP
        return {
            "ip_skipped": skipped,
            "status": "MATERIALS" if skipped else "IP",
        }

    def route_after_ip_choice(
        state: BrandWorkflowState,
    ) -> Literal["generate_ip", "generate_materials"]:
        return "generate_materials" if state.get("ip_skipped") else "generate_ip"

    def generate_ip(state: BrandWorkflowState) -> BrandWorkflowState:
        brand_spec = BrandSpec.model_validate(state["brand_spec"])
        draft = generate_text(
            state,
            capability=ModelCapability.IP,
            payload={
                "brand_spec": brand_spec.model_dump(mode="json"),
                "vi_output": state["vi_output"],
            },
            output_model=IPDraft,
            prompt_version="ip-v1",
        )
        source_logo = VIOutput.model_validate(state["vi_output"]).source_logo_asset_id
        output = IPOutput(
            **draft.model_dump(),
            preview_asset_id=generate_one_image(
                state,
                item_kind="ip",
                item_id="primary",
                prompt=draft.image_prompt,
                reference_asset_ids=[str(source_logo)],
            ),
        )
        return {"ip_output": output.model_dump(mode="json"), "status": "WAITING_USER"}

    def await_ip_decision(state: BrandWorkflowState) -> BrandWorkflowState:
        output = IPOutput.model_validate(state["ip_output"])
        decision = ConfirmStageDecision.model_validate(
            interrupt(
                {
                    "kind": "ip_decision",
                    "ip_output": output.model_dump(mode="json"),
                }
            )
        )
        return {
            "selected_version_ids": with_version(state, "IP", decision.version_id),
            "status": "MATERIALS",
        }

    def generate_materials(state: BrandWorkflowState) -> BrandWorkflowState:
        brand_spec = BrandSpec.model_validate(state["brand_spec"])
        vi_output = VIOutput.model_validate(state["vi_output"])
        used_asset_ids = [vi_output.source_logo_asset_id]
        if not state.get("ip_skipped", False):
            used_asset_ids.append(IPOutput.model_validate(state["ip_output"]).preview_asset_id)
        drafts = generate_text(
            state,
            capability=ModelCapability.MATERIALS,
            payload={
                "brand_spec": brand_spec.model_dump(mode="json"),
                "used_asset_ids": [str(item) for item in used_asset_ids],
            },
            output_model=MaterialDraftOutput,
            prompt_version="materials-v1",
        )
        asset_ids = generate_image_batch(
            state,
            item_kind="material",
            items=drafts.scenes,
            prompt_for=lambda draft: draft.image_prompt,
            references_for=lambda draft: [str(item) for item in draft.used_asset_ids],
        )
        scenes = [
            MaterialScene(
                **draft.model_dump(),
                preview_asset_id=asset_id,
            )
            for draft, asset_id in zip(drafts.scenes, asset_ids, strict=True)
        ]
        return {
            "material_output": MaterialOutput(scenes=scenes).model_dump(mode="json"),
            "status": "WAITING_USER",
        }

    def await_material_decision(state: BrandWorkflowState) -> BrandWorkflowState:
        output = MaterialOutput.model_validate(state["material_output"])
        decision = ConfirmStageDecision.model_validate(
            interrupt(
                {
                    "kind": "material_decision",
                    "material_output": output.model_dump(mode="json"),
                }
            )
        )
        return {
            "selected_version_ids": with_version(state, "MATERIALS", decision.version_id),
            "status": "REVIEW",
        }

    def generate_review(state: BrandWorkflowState) -> BrandWorkflowState:
        materials = MaterialOutput.model_validate(state["material_output"])
        review = generate_text(
            state,
            capability=ModelCapability.REVIEW,
            payload={
                "brand_spec": state["brand_spec"],
                "direction_output": state["direction_output"],
                "logo_output": state["logo_output"],
                "vi_output": state["vi_output"],
                "ip_output": state.get("ip_output"),
                "material_output": state["material_output"],
                "material_asset_ids": [str(item.preview_asset_id) for item in materials.scenes],
            },
            output_model=ReviewOutput,
            prompt_version="review-v1",
        )
        return {
            "review_output": review.model_dump(mode="json", by_alias=True),
            "status": "WAITING_USER",
        }

    def await_review_decision(state: BrandWorkflowState) -> BrandWorkflowState:
        output = ReviewOutput.model_validate(state["review_output"])
        decision = ReviewDecision.model_validate(
            interrupt(
                {
                    "kind": "review_decision",
                    "review_output": output.model_dump(mode="json", by_alias=True),
                }
            )
        )
        return {
            "selected_version_ids": with_version(state, "REVIEW", decision.version_id),
            "status": "PROPOSAL",
        }

    def generate_proposal(state: BrandWorkflowState) -> BrandWorkflowState:
        brand_spec = BrandSpec.model_validate(state["brand_spec"])
        directions = DirectionOutput.model_validate(state["direction_output"])
        selected_direction = next(
            item for item in directions.directions if item.id == state["selected_direction_id"]
        )
        logos = LogoOutput.model_validate(state["logo_output"])
        selected_logo = next(
            item for item in logos.concepts if item.id == state["selected_logo_id"]
        )
        materials = MaterialOutput.model_validate(state["material_output"])
        versions = state["selected_version_ids"]
        sections = [
            ProposalSection(
                type=ProposalSectionType.BRIEF,
                title="品牌简报",
                summary=directions.brief.brand_promise,
                version_id=versions["DIRECTIONS"],
            ),
            ProposalSection(
                type=ProposalSectionType.DIRECTION,
                title=selected_direction.name,
                summary=selected_direction.concept,
                version_id=versions["DIRECTIONS"],
                asset_ids=[selected_direction.preview_asset_id],
            ),
            ProposalSection(
                type=ProposalSectionType.LOGO,
                title=selected_logo.name,
                summary=selected_logo.rationale,
                version_id=versions["LOGO"],
                asset_ids=[selected_logo.preview_asset_id],
            ),
            ProposalSection(
                type=ProposalSectionType.VI,
                title="基础视觉规范",
                summary="包含色板、字体、Logo 使用规则和基础版式。",
                version_id=versions["VI"],
                asset_ids=[selected_logo.preview_asset_id],
            ),
        ]
        asset_refs = [
            selected_direction.preview_asset_id,
            selected_logo.preview_asset_id,
        ]
        if not state.get("ip_skipped", False):
            ip_output = IPOutput.model_validate(state["ip_output"])
            sections.append(
                ProposalSection(
                    type=ProposalSectionType.IP,
                    title=ip_output.character.name,
                    summary=ip_output.character.brand_connection,
                    version_id=versions["IP"],
                    asset_ids=[ip_output.preview_asset_id],
                )
            )
            asset_refs.append(ip_output.preview_asset_id)
        material_assets = [item.preview_asset_id for item in materials.scenes]
        sections.extend(
            [
                ProposalSection(
                    type=ProposalSectionType.MATERIALS,
                    title="品牌应用物料",
                    summary="展示两个预设场景中的品牌应用。",
                    version_id=versions["MATERIALS"],
                    asset_ids=material_assets,
                ),
                ProposalSection(
                    type=ProposalSectionType.REVIEW_SUMMARY,
                    title="审稿摘要",
                    summary=ReviewOutput.model_validate(state["review_output"]).summary,
                    version_id=versions["REVIEW"],
                ),
            ]
        )
        asset_refs.extend(material_assets)
        proposal_contract = ProposalOutput(
            title=f"{brand_spec.project_name} 品牌概念提案",
            narrative="从品牌需求出发，形成方向、标识、规范与应用的一致叙事。",
            sections=sections,
            asset_refs=asset_refs,
        )
        proposal = generate_text(
            state,
            capability=ModelCapability.PROPOSAL,
            payload={
                "brand_spec": brand_spec.model_dump(mode="json"),
                "proposal_contract": proposal_contract.model_dump(mode="json"),
            },
            output_model=ProposalOutput,
            prompt_version="proposal-v1",
        )
        return {
            "proposal_output": proposal.model_dump(mode="json"),
            "status": "WAITING_USER",
        }

    def await_proposal_decision(state: BrandWorkflowState) -> BrandWorkflowState:
        output = ProposalOutput.model_validate(state["proposal_output"])
        decision = ConfirmStageDecision.model_validate(
            interrupt(
                {
                    "kind": "proposal_decision",
                    "proposal_output": output.model_dump(mode="json"),
                }
            )
        )
        return {
            "selected_version_ids": with_version(state, "PROPOSAL", decision.version_id),
            "status": "EXPORT_READY",
        }

    builder = StateGraph(BrandWorkflowState)
    builder.add_node("analyze_intake", analyze_intake)
    builder.add_node("await_intake_answers", await_intake_answers)
    builder.add_node("generate_directions", generate_directions)
    builder.add_node("await_direction_decision", await_direction_decision)
    builder.add_node("generate_logo", generate_logo)
    builder.add_node("await_logo_decision", await_logo_decision)
    builder.add_node("generate_vi", generate_vi)
    builder.add_node("await_vi_decision", await_vi_decision)
    builder.add_node("await_ip_choice", await_ip_choice)
    builder.add_node("generate_ip", generate_ip)
    builder.add_node("await_ip_decision", await_ip_decision)
    builder.add_node("generate_materials", generate_materials)
    builder.add_node("await_material_decision", await_material_decision)
    builder.add_node("generate_review", generate_review)
    builder.add_node("await_review_decision", await_review_decision)
    builder.add_node("generate_proposal", generate_proposal)
    builder.add_node("await_proposal_decision", await_proposal_decision)

    builder.add_edge(START, "analyze_intake")
    builder.add_conditional_edges("analyze_intake", route_after_intake)
    builder.add_edge("await_intake_answers", "analyze_intake")
    builder.add_edge("generate_directions", "await_direction_decision")
    builder.add_edge("await_direction_decision", "generate_logo")
    builder.add_edge("generate_logo", "await_logo_decision")
    builder.add_edge("await_logo_decision", "generate_vi")
    builder.add_edge("generate_vi", "await_vi_decision")
    builder.add_edge("await_vi_decision", "await_ip_choice")
    builder.add_conditional_edges("await_ip_choice", route_after_ip_choice)
    builder.add_edge("generate_ip", "await_ip_decision")
    builder.add_edge("await_ip_decision", "generate_materials")
    builder.add_edge("generate_materials", "await_material_decision")
    builder.add_edge("await_material_decision", "generate_review")
    builder.add_edge("generate_review", "await_review_decision")
    builder.add_edge("await_review_decision", "generate_proposal")
    builder.add_edge("generate_proposal", "await_proposal_decision")
    builder.add_edge("await_proposal_decision", END)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before),
    )
